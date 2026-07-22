#!/usr/bin/env bash
# ============================================================
#  Dispare v4.1 — установщик hardening прямо на сервере.
#  Кладёт правильные файлы в нужные папки, проверяет их и деплоит.
#  Запуск:  bash install_v4.sh
# ============================================================
set -u

# 1) Найти папку проекта
if [ -f app/main.py ]; then
  PROJ="$(pwd)"
elif [ -d /root/dispare-tool/dispare-tool-v3 ]; then
  PROJ="/root/dispare-tool/dispare-tool-v3"
else
  echo "Не нашёл папку проекта. Перейдите в неё:  cd /root/dispare-tool/dispare-tool-v3  и запустите снова."
  exit 1
fi
cd "$PROJ" || exit 1
echo "Папка проекта: $PROJ"

# 2) Бэкап базы ДО любых изменений (через работающий контейнер)
echo "=== Резервная копия базы ==="
if docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('data/dispare.db'); d=sqlite3.connect('data/backup_before_v4.db'); s.backup(d); d.close(); s.close(); print('backup ok')" 2>/dev/null; then
  echo "  копия: data/backup_before_v4.db"
else
  echo "  ВНИМАНИЕ: не удалось сделать копию через контейнер (возможно, он не запущен)."
  echo "  Пробую скопировать файл базы напрямую..."
  [ -f data/dispare.db ] && cp -a data/dispare.db data/backup_before_v4.db && echo "  копия сделана напрямую" || echo "  базы нет — пропускаю."
fi

# 3) Записать правильные файлы
echo "=== Запись файлов ==="
mkdir -p "$(dirname "app/config.py")"
cat > "app/config.py" << 'DISPARE_EOF_MARKER'
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    """Обязательная переменная окружения. Если её нет — приложение не стартует
    (лучше явная ошибка, чем тихий запуск с небезопасным значением по умолчанию)."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Добавьте её в файл .env и перезапустите. "
            f"Для SECRET_KEY можно сгенерировать: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return val


# --- Безопасность: обязательные секреты, без небезопасных значений по умолчанию ---
SECRET_KEY = _require("SECRET_KEY")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/dispare.db")

# Emex
EMEX_API_KEY = os.getenv("EMEX_API_KEY", "")
EMEX_COST_PER_REQUEST = float(os.getenv("EMEX_COST_PER_REQUEST", "0.12"))
EMEX_DAILY_BUDGET = float(os.getenv("EMEX_DAILY_BUDGET", "200"))
EMEX_MONTHLY_BUDGET = float(os.getenv("EMEX_MONTHLY_BUDGET", "5000"))
EMEX_CACHE_TTL_HOURS = int(os.getenv("EMEX_CACHE_TTL_HOURS", "24"))

# ZZap
ZZAP_LOGIN = os.getenv("ZZAP_LOGIN", "")
ZZAP_API_KEY = os.getenv("ZZAP_API_KEY", "")

# Admin.
# ADMIN_PASSWORD нужен ТОЛЬКО для первого создания пользователя admin.
# Если admin уже есть в базе — переменная не используется и её можно удалить из .env.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # None, если не задан — это нормально
DISPARE_EOF_MARKER
echo "  записан: app/config.py"
mkdir -p "$(dirname "app/auth.py")"
cat > "app/auth.py" << 'DISPARE_EOF_MARKER'
import re
import hashlib
import secrets
from datetime import datetime, timedelta
from jose import jwt
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from app.config import SECRET_KEY
from app.database import get_db
from app.models import User

ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30
COOKIE_NAME = "dispare_token"

# Современный хешер паролей. Argon2id — медленный намеренно, чтобы перебор был дорогим.
_ph = PasswordHasher()


def hash_password(password: str) -> str:
    """Новые пароли хешируются Argon2id. Результат вида $argon2id$v=19$..."""
    return _ph.hash(password)


def _is_argon2(hashed: str) -> bool:
    return hashed.startswith("$argon2")


def _verify_legacy_sha256(plain: str, hashed: str) -> bool:
    """Старая схема: salt$sha256(salt+password). Поддерживаем для входа старых
    пользователей — при успешном входе их хеш автоматически обновится на Argon2id."""
    parts = hashed.split("$")
    if len(parts) != 2:
        return False
    salt, h = parts
    return hashlib.sha256((salt + plain).encode()).hexdigest() == h


def verify_password(plain: str, hashed: str) -> bool:
    """Проверяет пароль против ЛЮБОГО формата хеша (Argon2id или старый SHA-256)."""
    if not hashed:
        return False
    if _is_argon2(hashed):
        try:
            return _ph.verify(hashed, plain)
        except (VerifyMismatchError, InvalidHashError, Exception):
            return False
    return _verify_legacy_sha256(plain, hashed)


def needs_rehash(hashed: str) -> bool:
    """True, если хеш стоит пересчитать (старый SHA-256 или устаревшие параметры Argon2)."""
    if not hashed or not _is_argon2(hashed):
        return True
    try:
        return _ph.check_needs_rehash(hashed)
    except Exception:
        return True


def create_token(user_id: int, username: str) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = db.query(User).filter(User.id == int(payload["sub"])).first()
        if not user:
            raise HTTPException(status_code=401)
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def clean_part_number(pn: str) -> str:
    return re.sub(r"[\s\-\.\/]", "", pn).upper()


def ensure_admin(db: Session):
    """Создаёт пользователя admin ТОЛЬКО если его ещё нет.
    Существующий admin не трогается — его пароль/хеш сохраняется."""
    from app.config import ADMIN_USERNAME, ADMIN_PASSWORD
    existing = db.query(User).filter(User.username == ADMIN_USERNAME).first()
    if existing:
        return
    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "Пользователь admin отсутствует в базе, а ADMIN_PASSWORD не задан. "
            "Задайте ADMIN_PASSWORD в .env для первого создания admin, "
            "затем удалите его и смените пароль через: "
            "docker compose exec app python -m app.set_password"
        )
    admin = User(
        username=ADMIN_USERNAME,
        password_hash=hash_password(ADMIN_PASSWORD),
        is_admin=True,
    )
    db.add(admin)
    db.commit()
DISPARE_EOF_MARKER
echo "  записан: app/auth.py"
mkdir -p "$(dirname "app/set_password.py")"
cat > "app/set_password.py" << 'DISPARE_EOF_MARKER'
"""Безопасная смена пароля пользователя (по умолчанию — admin).

Запуск на сервере:
    docker compose exec app python -m app.set_password

Пароль вводится в терминале и НЕ отображается на экране, не пишется в файлы,
не попадает в логи. В базе хранится только необратимый Argon2id-хеш.

Указать другого пользователя:
    docker compose exec app python -m app.set_password --user someuser
"""
import sys
import getpass

from app.database import SessionLocal, init_db
from app.models import User
from app.auth import hash_password


def main():
    username = "admin"
    if "--user" in sys.argv:
        i = sys.argv.index("--user")
        if i + 1 < len(sys.argv):
            username = sys.argv[i + 1]

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"Пользователь '{username}' не найден в базе.")
            sys.exit(1)

        pw1 = getpass.getpass("Новый пароль: ")
        if len(pw1) < 8:
            print("Слишком короткий пароль. Минимум 8 символов.")
            sys.exit(1)
        pw2 = getpass.getpass("Повторите пароль: ")
        if pw1 != pw2:
            print("Пароли не совпадают.")
            sys.exit(1)

        user.password_hash = hash_password(pw1)
        db.commit()
        print(f"Пароль пользователя '{username}' изменён. Хеш обновлён на Argon2id.")
        print("Теперь можно удалить ADMIN_PASSWORD из .env — он больше не нужен.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
DISPARE_EOF_MARKER
echo "  записан: app/set_password.py"
mkdir -p "$(dirname "app/backup_db.py")"
cat > "app/backup_db.py" << 'DISPARE_EOF_MARKER'
"""Безопасная резервная копия SQLite-базы.

Использует официальный backup API SQLite (корректно при включённом WAL —
простой cp может скопировать базу в несогласованном состоянии).

Запуск на сервере:
    docker compose exec app python -m app.backup_db

Копии складываются в data/backups/ (это смонтированный на хост том, т.е.
они переживают пересборку контейнера). Хранятся последние 30 копий.
Рекомендуется дополнительно копировать эту папку на внешний сервер/облако.
"""
import os
import glob
import sqlite3
import datetime

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "dispare.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
KEEP = 30


def main():
    if not os.path.exists(DB_PATH):
        print(f"База не найдена: {DB_PATH}")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(BACKUP_DIR, f"dispare_{stamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    size_kb = os.path.getsize(dest_path) / 1024
    print(f"Копия создана: {dest_path} ({size_kb:.0f} КБ)")

    # Чистим старые копии, оставляем последние KEEP
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "dispare_*.db")))
    for old in files[:-KEEP]:
        os.remove(old)
        print(f"Удалена старая копия: {os.path.basename(old)}")


if __name__ == "__main__":
    main()
DISPARE_EOF_MARKER
echo "  записан: app/backup_db.py"
mkdir -p "$(dirname "app/models.py")"
cat > "app/models.py" << 'DISPARE_EOF_MARKER'
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Date, Text, UniqueConstraint, Index
)
from sqlalchemy.sql import func
from app.database import Base


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    currency = Column(String(10), default="USD")
    notes = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


class CatalogItem(Base):
    """Позиции из прайс-листов поставщиков (до 100k строк)."""
    __tablename__ = "catalog_items"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, nullable=False)
    part_number = Column(String(100), nullable=False, index=True)
    part_number_clean = Column(String(100), nullable=False, index=True)  # без пробелов/тире
    brand = Column(String(200), default="")
    description = Column(Text, default="")
    purchase_price = Column(Float, default=0)
    currency = Column(String(10), default="USD")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("supplier_id", "part_number_clean", name="uq_supplier_part"),
    )


class OurPrice(Base):
    """Наши продажные цены (3 вида)."""
    __tablename__ = "our_prices"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    price_order = Column(Float, default=0)       # под заказ, без эмекс и склада
    price_3pl = Column(Float, default=0)          # со склада 3PL, без эмекс
    price_3pl_emex = Column(Float, default=0)     # 3PL + эмекс
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MarketPrice(Base):
    """История рыночных цен (Emex и другие источники)."""
    __tablename__ = "market_prices"
    id = Column(Integer, primary_key=True)
    part_number_clean = Column(String(100), nullable=False, index=True)
    source = Column(String(50), nullable=False, default="emex")
    price = Column(Float)
    delivery_days = Column(Integer, default=0)
    quantity = Column(Integer, default=0)
    seller = Column(String(300), default="")
    rating = Column(Float, default=0)
    fetched_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_market_pn_date", "part_number_clean", "fetched_at"),
    )


class Watchlist(Base):
    """Артикулы на автоматическом мониторинге Emex."""
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    frequency = Column(String(20), default="daily")  # daily / twice_daily
    last_checked = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    id = Column(Integer, primary_key=True)
    currency = Column(String(10), nullable=False)
    rate_to_rub = Column(Float, nullable=False)
    date = Column(String(10), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("currency", "date", name="uq_currency_date"),
    )


class EmexBudget(Base):
    __tablename__ = "emex_budget"
    id = Column(Integer, primary_key=True)
    date = Column(String(10), nullable=False, unique=True)
    requests_count = Column(Integer, default=0)
    cost_rub = Column(Float, default=0)


class Inventory(Base):
    """Склад: остатки, полная себестоимость, авто-цены."""
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    description = Column(Text, default="")
    quantity = Column(Integer, default=0)
    # Закупка
    purchase_price = Column(Float, default=0)         # цена за единицу в валюте
    purchase_currency = Column(String(10), default="USD")
    purchase_rate = Column(Float, default=0)           # курс на дату закупки
    # Расходы на единицу (в рублях)
    logistics_cost = Column(Float, default=0)          # логистика
    customs_duty = Column(Float, default=0)            # таможенная пошлина
    vat_cost = Column(Float, default=0)                # НДС
    warehouse_cost = Column(Float, default=0)          # склад/3PL
    other_costs = Column(Float, default=0)             # прочие расходы
    # Рассчитанная себестоимость (закупка*курс + все расходы)
    cost_rub = Column(Float, default=0)
    # Наценки
    markup_mode = Column(String(10), default="pct")    # pct / rub / manual
    markup_order_pct = Column(Float, default=30)
    markup_3pl_pct = Column(Float, default=35)
    markup_3pl_emex_pct = Column(Float, default=40)
    markup_order_rub = Column(Float, default=0)
    markup_3pl_rub = Column(Float, default=0)
    markup_3pl_emex_rub = Column(Float, default=0)
    # Продажные цены
    price_order = Column(Float, default=0)       # Заказ под клиента
    price_3pl = Column(Float, default=0)         # Продажа со склада 3PL
    price_3pl_emex = Column(Float, default=0)    # Продажа через EMEX
    last_rate_used = Column(Float, default=0)
    first_stock_date = Column(Date)               # когда позиция впервые появилась на складе
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockTransaction(Base):
    """Движение товаров: продажа, возврат, поступление, корректировка."""
    __tablename__ = "stock_transactions"
    id = Column(Integer, primary_key=True)
    part_number_clean = Column(String(100), nullable=False, index=True)
    tx_type = Column(String(20), nullable=False)  # receipt / sale / return / adjust
    quantity = Column(Integer, nullable=False)
    price = Column(Float, default=0)
    op_date = Column(Date, index=True)            # ДАТА СОБЫТИЯ (когда Emex купил), не дата записи
    cost_at_sale = Column(Float, default=0)       # СНИМОК себестоимости на момент продажи
    batch_id = Column(Integer, default=0)         # из какой партии ушло (для ФИФО)
    sale_rate = Column(Float, default=0)          # курс USD/₽ на дату операции (для валютной переоценки)
    notes = Column(Text, default="")
    username = Column(String(100), default="")
    created_at = Column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(200), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())


class EmexSetting(Base):
    """Ключ-значение для Emex-раздела: комиссия, страховка, сортировка,
    логистика, секретный токен прайса."""
    __tablename__ = "emex_settings"
    id = Column(Integer, primary_key=True)
    key = Column(String(50), nullable=False, unique=True)
    value = Column(String(200), default="")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class EmexOrderLog(Base):
    """Журнал обработанных заказов Emex — защита от повторной загрузки.

    Таблица только для дозаписи (не связана с продажами, ничего не ломает).
    Перед обработкой заказа проверяем: не грузили ли уже этот номер / этот файл.
    Создаётся автоматически при старте приложения — базу пересоздавать НЕ нужно.
    """
    __tablename__ = "emex_order_log"
    id = Column(Integer, primary_key=True)
    order_no = Column(String(20), index=True, default="")   # номер заказа Emex из файла
    op_date = Column(Date)                                    # дата заказа
    file_hash = Column(String(64), index=True)               # sha256 содержимого файла
    articles = Column(Integer, default=0)                    # позиций
    units = Column(Integer, default=0)                       # штук
    username = Column(String(100), default="")
    created_at = Column(DateTime, server_default=func.now())


class Batch(Base):
    """Партия закупки (первая авиа, вторая и т.д.)."""
    __tablename__ = "batches"
    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    arrival_date = Column(Date)                    # когда приехала на склад
    start_sale_date = Column(Date)                 # с какой даты продаём (для оборачиваемости)
    note = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


class BatchLot(Base):
    """Поступление конкретного артикула в партии.
    Хранится всегда — это задел под ФИФО: даже считая по средней,
    мы знаем, сколько и по какой цене приехало в каждой партии."""
    __tablename__ = "batch_lots"
    id = Column(Integer, primary_key=True)
    batch_id = Column(Integer, index=True, nullable=False)
    part_number_clean = Column(String(100), index=True, nullable=False)
    qty_in = Column(Integer, default=0)            # приехало
    qty_left = Column(Integer, default=0)          # осталось от этой партии (для ФИФО)
    cost_rub = Column(Float, default=0)            # себестоимость единицы В ЭТОЙ партии
    received_at = Column(Date)
DISPARE_EOF_MARKER
echo "  записан: app/models.py"
mkdir -p "$(dirname "app/main.py")"
cat > "app/main.py" << 'DISPARE_EOF_MARKER'
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.database import init_db, SessionLocal
from app.auth import ensure_admin
from app.routers import pages, api_catalog, api_prices, api_search, api_market, api_inventory, emex


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    db = SessionLocal()
    ensure_admin(db)
    db.close()

    # Запускаем фоновые задачи (обновление курсов, мониторинг watchlist)
    from app.scheduler import start_scheduler
    scheduler = start_scheduler()

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Dispare Trading — Проценка",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
def health():
    """Проверка живости для Docker healthcheck. Пингует базу."""
    from sqlalchemy import text
    from app.database import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "error", "database": str(e)}, status_code=503)

# Роутеры
app.include_router(pages.router)
app.include_router(api_catalog.router)
app.include_router(api_prices.router)
app.include_router(api_search.router)
app.include_router(api_market.router)
app.include_router(api_inventory.router)
app.include_router(emex.router)
DISPARE_EOF_MARKER
echo "  записан: app/main.py"
mkdir -p "$(dirname "app/migrate.py")"
cat > "app/migrate.py" << 'DISPARE_EOF_MARKER'
"""Обновление базы под новую версию — БЕЗ потери данных.

Что делает:
  • добавляет в продажи дату события (op_date), снимок себестоимости (cost_at_sale)
    и партию (batch_id);
  • восстанавливает настоящие даты продаж из примечаний («Emex заказ 13.07.2026»),
    т.к. раньше бралась дата загрузки файла, а не дата заказа;
  • добавляет в склад дату появления товара (first_stock_date);
  • создаёт таблицы партий (batches, batch_lots);
  • проставляет снимок себестоимости старым продажам (по текущей себестоимости);
  • создаёт «Партию №1» из того, что уже лежит на складе, и ставит дату старта.

Запуск:
    docker compose exec app python -m app.migrate
Запускать можно повторно — лишнего не сделает.
"""
import datetime
from sqlalchemy import text

from app.database import engine, SessionLocal, Base
from app.models import Inventory, StockTransaction, Batch, BatchLot, EmexOrderLog

START_DEFAULT = datetime.date(2026, 7, 9)   # дата старта продаж первой партии


def _cols(conn, table):
    return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def main():
    # 1) новые таблицы
    Base.metadata.create_all(bind=engine)

    # 2) новые колонки в существующих таблицах
    with engine.begin() as conn:
        st = _cols(conn, "stock_transactions")
        if "cost_at_sale" not in st:
            conn.execute(text("ALTER TABLE stock_transactions ADD COLUMN cost_at_sale FLOAT DEFAULT 0"))
            print("+ stock_transactions.cost_at_sale")
        if "batch_id" not in st:
            conn.execute(text("ALTER TABLE stock_transactions ADD COLUMN batch_id INTEGER DEFAULT 0"))
            print("+ stock_transactions.batch_id")
        if "op_date" not in st:
            conn.execute(text("ALTER TABLE stock_transactions ADD COLUMN op_date DATE"))
            print("+ stock_transactions.op_date")
        inv = _cols(conn, "inventory")
        if "first_stock_date" not in inv:
            conn.execute(text("ALTER TABLE inventory ADD COLUMN first_stock_date DATE"))
            print("+ inventory.first_stock_date")

    db = SessionLocal()
    try:
        # 3) настоящая дата продажи из примечания («Emex заказ 13.07.2026»)
        import re
        dated = 0
        for s in db.query(StockTransaction).all():
            if s.op_date:
                continue
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s.notes or "")
            if m:
                s.op_date = datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                dated += 1
            elif s.created_at:
                s.op_date = s.created_at.date()
                dated += 1
        if dated:
            print(f"= восстановлена дата продажи у {dated} записей")

        # 3.1) дописать номер заказа старым продажам (нумеруем заказы по датам)
        import re as _re
        no_num = [s for s in db.query(StockTransaction).all()
                  if s.tx_type == "sale" and "№" not in (s.notes or "")]
        if no_num:
            # уникальные даты продаж -> порядковый номер заказа
            dates = sorted({s.op_date for s in db.query(StockTransaction).all()
                            if s.tx_type == "sale" and s.op_date})
            # известные соответствия дата->номер из реальных заказов
            known = {datetime.date(2026,7,10):"1", datetime.date(2026,7,13):"2",
                     datetime.date(2026,7,15):"4", datetime.date(2026,7,16):"5"}
            for s in no_num:
                num = known.get(s.op_date)
                if not num and s.op_date in dates:
                    num = str(dates.index(s.op_date) + 1)
                if num:
                    d = s.op_date.strftime("%d.%m.%Y") if s.op_date else ""
                    s.notes = f"Emex заказ №{num} от {d}"
            print(f"= проставлен номер заказа у {len(no_num)} записей")

        # 4) снимок себестоимости старым продажам
        costs = {i.part_number_clean: (i.cost_rub or 0) for i in db.query(Inventory).all()}
        fixed = 0
        for s in db.query(StockTransaction).filter(StockTransaction.tx_type == "sale").all():
            if not s.cost_at_sale:
                s.cost_at_sale = costs.get(s.part_number_clean, 0)
                fixed += 1
        if fixed:
            print(f"= проставлен снимок себестоимости у {fixed} продаж")

        # 5) дата появления на складе
        no_date = db.query(Inventory).filter(Inventory.first_stock_date.is_(None)).all()
        for i in no_date:
            i.first_stock_date = START_DEFAULT
        if no_date:
            print(f"= дата старта {START_DEFAULT:%d.%m.%Y} у {len(no_date)} позиций")

        # 6) «Партия №1» из текущего склада (если партий ещё нет)
        if not db.query(Batch).count():
            items = db.query(Inventory).all()
            if items:
                b = Batch(name="Партия №1 (первая, авиа)", arrival_date=START_DEFAULT,
                          start_sale_date=START_DEFAULT,
                          note="Создана автоматически из текущих остатков склада")
                db.add(b)
                db.flush()
                sold = {}
                for s in db.query(StockTransaction).filter(StockTransaction.tx_type == "sale").all():
                    sold[s.part_number_clean] = sold.get(s.part_number_clean, 0) + abs(s.quantity or 0)
                for i in items:
                    qty_in = (i.quantity or 0) + sold.get(i.part_number_clean, 0)  # сколько приехало
                    db.add(BatchLot(batch_id=b.id, part_number_clean=i.part_number_clean,
                                    qty_in=qty_in, qty_left=i.quantity or 0,
                                    cost_rub=i.cost_rub or 0, received_at=START_DEFAULT))
                print(f"+ Партия №1: {len(items)} позиций, приехало {sum((i.quantity or 0) + sold.get(i.part_number_clean, 0) for i in items)} шт")
        # 7) заполнить журнал заказов из уже обработанных продаж
        #    (чтобы защита от повторной загрузки работала и для старых заказов)
        if not db.query(EmexOrderLog).count():
            sales = db.query(StockTransaction).filter(StockTransaction.tx_type == "sale").all()
            orders = {}  # order_no -> {op_date, articles(set), units}
            for s in sales:
                note = s.notes or ""
                mno = re.search(r"№\s*(\d+)", note)
                order_no = mno.group(1) if mno else ""
                md = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", note)
                op_date = s.op_date
                if not op_date and md:
                    op_date = datetime.date(int(md.group(3)), int(md.group(2)), int(md.group(1)))
                key = order_no or (op_date.isoformat() if op_date else f"tx{s.id}")
                o = orders.setdefault(key, {"order_no": order_no, "op_date": op_date,
                                            "arts": set(), "units": 0})
                o["arts"].add(s.part_number_clean)
                o["units"] += abs(s.quantity or 0)
            for o in orders.values():
                db.add(EmexOrderLog(
                    order_no=o["order_no"], op_date=o["op_date"], file_hash="",
                    articles=len(o["arts"]), units=o["units"], username="migrate",
                ))
            if orders:
                print(f"+ журнал заказов: занесено {len(orders)} прошлых заказов (защита от повтора)")

        db.commit()
        print("Готово. База обновлена, данные на месте.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
DISPARE_EOF_MARKER
echo "  записан: app/migrate.py"
mkdir -p "$(dirname "app/routers/emex.py")"
cat > "app/routers/emex.py" << 'DISPARE_EOF_MARKER'
"""Emex-раздел: секретный прайс по ссылке, обработка заказа, дашборд продаж."""
import os
import secrets
import hashlib
import datetime
from io import BytesIO

from fastapi import APIRouter, Request, Depends, UploadFile, File, Body, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill

from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import User, Inventory, StockTransaction, EmexSetting, MarketPrice, Batch, BatchLot, EmexOrderLog
from app.services import emex_order, batches

router = APIRouter(tags=["emex"])
templates = Jinja2Templates(directory="app/templates")

OUT_DIR = "data/emex_out"
# Emex НЕ удерживает с нас комиссию/страховку/сортировку — он накручивает их
# СВЕРХУ покупателю. Наш расход — только стикеровка (Emex удерживает из платежа).
DEFAULTS = {
    "sticker": "39",        # ₽/шт, с НДС (входящий НДС возмещается)
    "sticker_vat": "1",     # 1 = в стикеровке есть НДС 22% (возмещаемый)
    "markup": "1",          # наша наценка в настройках Emex, %
    "shelf_markup": "46",   # накрутка Emex для анонимного покупателя, % (справочно)
    "start_date": "2026-07-09",   # дата старта продаж (для оборачиваемости)
}


def _user_or_none(request: Request, db: Session):
    try:
        return get_current_user(request, db)
    except Exception:
        return None


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.query(EmexSetting).filter(EmexSetting.key == key).first()
    return row.value if row else default


def set_setting(db: Session, key: str, value: str):
    row = db.query(EmexSetting).filter(EmexSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(EmexSetting(key=key, value=value))
    db.commit()


def get_or_create_token(db: Session) -> str:
    tok = get_setting(db, "price_token", "")
    if not tok:
        tok = secrets.token_hex(6)
        set_setting(db, "price_token", tok)
    return tok


# ---------------- страница дашборда ----------------
@router.get("/emex", response_class=HTMLResponse)
def emex_page(request: Request, db: Session = Depends(get_db)):
    user = _user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    token = get_or_create_token(db)
    return templates.TemplateResponse("emex.html", {
        "request": request, "user": user, "price_token": token,
    })


# ---------------- данные для дашборда (JSON) ----------------
@router.get("/emex/data")
def emex_data(request: Request, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    inv = db.query(Inventory).all()
    by_clean = {i.part_number_clean: i for i in inv}
    market = {}
    for m in db.query(MarketPrice).order_by(MarketPrice.fetched_at.asc()).all():
        market[m.part_number_clean] = m.price

    catalog = []
    for i in inv:
        # сколько приехало всего (по всем партиям) — для «продано % от партии»
        lots = db.query(BatchLot).filter(BatchLot.part_number_clean == i.part_number_clean).all()
        init = sum(l.qty_in for l in lots) or (i.quantity or 0)
        catalog.append({
            "art": i.part_number, "clean": i.part_number_clean,
            "type": i.description or i.brand or "",
            "cost": round(i.cost_rub or 0, 2),
            "price": round(i.price_3pl_emex or 0, 2),
            "stock": i.quantity or 0,
            "init": init,
            "since": i.first_stock_date.isoformat() if i.first_stock_date else None,
            "market": round(market[i.part_number_clean], 2) if market.get(i.part_number_clean) else None,
        })

    sales = []
    q = (db.query(StockTransaction)
           .filter(StockTransaction.tx_type == "sale")
           .order_by(StockTransaction.created_at.asc()))
    for s_ in q.all():
        item = by_clean.get(s_.part_number_clean)
        d_ = s_.op_date or (s_.created_at.date() if s_.created_at else None)
        sales.append({
            "date": (d_.isoformat() if d_ else ""),
            "art": item.part_number if item else s_.part_number_clean,
            "qty": abs(s_.quantity or 0),
            "price": round(s_.price or 0, 2),
            # СНИМОК себестоимости на момент продажи; если пусто (старые записи) — текущая
            "cost": round(s_.cost_at_sale or (item.cost_rub if item else 0) or 0, 2),
            "note": s_.notes or "",
        })

    settings = {k: get_setting(db, k, v) for k, v in DEFAULTS.items()}
    batch_list = [{"id": b.id, "name": b.name,
                   "arrival": b.arrival_date.isoformat() if b.arrival_date else None,
                   "start": b.start_sale_date.isoformat() if b.start_sale_date else None}
                  for b in db.query(Batch).order_by(Batch.id.asc()).all()]
    return {"catalog": catalog, "sales": sales, "settings": settings,
            "batches": batch_list, "token": get_or_create_token(db)}


# ---------------- приём партии ----------------
@router.post("/emex/batch-preview")
async def batch_preview(file: UploadFile = File(...), db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """Показывает, что произойдёт, НЕ меняя базу."""
    try:
        rows = batches.parse_batch_file(await file.read())
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Не читается файл: {e}"}, status_code=400)
    prev = []
    for r in rows:
        if r["qty"] <= 0:
            continue
        item = db.query(Inventory).filter(Inventory.part_number_clean == r["clean"]).first()
        if item:
            oq, oc = item.quantity or 0, item.cost_rub or 0
            nq = oq + r["qty"]
            nc = round((oq * oc + r["qty"] * r["cost"]) / nq, 2) if nq else r["cost"]
            prev.append({"art": r["art"], "new": False, "old_qty": oq, "in_qty": r["qty"],
                         "qty": nq, "old_cost": round(oc, 2), "in_cost": round(r["cost"], 2), "cost": nc})
        else:
            prev.append({"art": r["art"], "new": True, "old_qty": 0, "in_qty": r["qty"],
                         "qty": r["qty"], "old_cost": 0, "in_cost": round(r["cost"], 2),
                         "cost": round(r["cost"], 2)})
    return {"ok": True, "rows": prev}


@router.post("/emex/batch-receive")
async def batch_receive(file: UploadFile = File(...), name: str = Form(...),
                        arrival: str = Form(...), start_sale: str = Form(""),
                        db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        rows = batches.parse_batch_file(await file.read())
        ad = datetime.date.fromisoformat(arrival)
        sd = datetime.date.fromisoformat(start_sale) if start_sale else ad
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Ошибка: {e}"}, status_code=400)
    batch, report = batches.receive_batch(db, rows, name, ad, sd)
    return {"ok": True, "batch": batch.name, "rows": report,
            "added": sum(1 for r in report if r["new"]),
            "merged": sum(1 for r in report if not r["new"])}


# ---------------- загрузка заказа ----------------
@router.post("/emex/upload")
async def emex_upload(request: Request, file: UploadFile = File(...),
                      force: str = Form("0"),
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    content = await file.read()
    try:
        order_date, order_no, lines = emex_order.parse_lqld(content)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Не удалось прочитать файл заказа: {e}"}, status_code=400)
    if not lines:
        return JSONResponse({"ok": False, "error": "В файле не найдено строк заказа."}, status_code=400)

    # --- Защита от повторной загрузки ---
    # Считаем заказ уже обработанным, если совпал либо номер заказа, либо хеш файла.
    file_hash = hashlib.sha256(content).hexdigest()
    force_flag = str(force) in ("1", "true", "on", "yes")
    if not force_flag:
        dup = None
        if order_no:
            dup = db.query(EmexOrderLog).filter(EmexOrderLog.order_no == order_no).first()
        if not dup:
            dup = db.query(EmexOrderLog).filter(EmexOrderLog.file_hash == file_hash).first()
        if dup:
            return JSONResponse({
                "ok": False,
                "duplicate": True,
                "error": (f"Заказ №{dup.order_no or '—'} уже обрабатывался "
                          f"{dup.created_at.strftime('%d.%m.%Y %H:%M') if dup.created_at else ''} "
                          f"({dup.articles} позиций, {dup.units} шт). "
                          f"Повторная загрузка спишет остатки ещё раз."),
                "order_no": dup.order_no,
                "processed_at": dup.created_at.strftime("%d.%m.%Y %H:%M") if dup.created_at else "",
                "hint": "Если это действительно нужно (например, исправленная версия) — "
                        "повторите загрузку с подтверждением (force).",
            }, status_code=409)

    cons = emex_order.consolidate(lines)
    changes, missing = emex_order.apply_order_to_inventory(db, cons, order_date, user.username, order_no)

    # Фиксируем факт обработки заказа в журнале (после успешного списания)
    db.add(EmexOrderLog(
        order_no=order_no or "",
        op_date=order_date,
        file_hash=file_hash,
        articles=len(cons),
        units=sum(v["qty"] for v in cons.values()),
        username=user.username,
    ))
    db.commit()

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = order_date.strftime("%d%m%y")
    wh_name = f"Отгрузка_ДИСПЭР_{stamp}.xlsx"
    ac_name = f"Задание_бухгалтеру_{stamp}.xlsx"
    with open(os.path.join(OUT_DIR, wh_name), "wb") as f:
        f.write(emex_order.build_warehouse_xlsx(cons, order_date))
    with open(os.path.join(OUT_DIR, ac_name), "wb") as f:
        f.write(emex_order.build_accountant_xlsx(lines))

    return {
        "ok": True,
        "order_date": order_date.strftime("%d.%m.%Y"),
        "req_no": f"1/{stamp}",
        "order_no": order_no,
        "deliver": emex_order.next_business_day(order_date).strftime("%d.%m.%Y"),
        "articles": len(cons),
        "units": sum(v["qty"] for v in cons.values()),
        "changes": [{"art": a, "old": o, "sold": s, "new": n} for a, o, s, n in changes],
        "missing": [{"art": a, "sold": s} for a, s in missing],
        "warehouse_file": wh_name,
        "accountant_file": ac_name,
    }


@router.get("/emex/download")
def emex_download(name: str, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    safe = os.path.basename(name)
    path = os.path.join(OUT_DIR, safe)
    if not os.path.exists(path):
        return JSONResponse({"error": "Файл не найден"}, status_code=404)
    return FileResponse(path, filename=safe,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------------- сохранить цены (полуручной прайс) ----------------
@router.post("/emex/save-price")
def emex_save_price(payload: dict = Body(...), db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    n = 0
    for it in payload.get("items", []):
        clean = clean_part_number(str(it.get("art", "")))
        item = db.query(Inventory).filter(Inventory.part_number_clean == clean).first()
        if item and it.get("price") is not None:
            item.price_3pl_emex = round(float(it["price"]), 2)
            item.markup_mode = "manual"  # чтобы авто-пересчёт не перетёр вашу цену
            n += 1
    db.commit()
    return {"ok": True, "updated": n}


# ---------------- импорт цен из файла прайса ----------------
@router.post("/emex/import-prices")
async def emex_import_prices(file: UploadFile = File(...), apply: str = Form("0"),
                             db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    """Читает файл прайса (№ детали | Марка | Цена детали | Количество)
    и обновляет ТОЛЬКО цены. Остатки не трогает — они ведутся заказами.
    apply=0 — показать разницу, apply=1 — записать."""
    content = await file.read()
    try:
        if content[:2] == b"PK":
            df = pd.read_excel(BytesIO(content), header=0, engine="openpyxl")
        else:
            df = pd.read_excel(BytesIO(content), header=0, engine="xlrd")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Не читается файл: {e}"}, status_code=400)

    cols = list(df.columns)
    if len(cols) < 3:
        return JSONResponse({"ok": False, "error": "Ожидаются колонки: № детали, Марка, Цена детали"}, status_code=400)

    rows, missing = [], []
    for _, r in df.iterrows():
        raw = r[cols[0]]
        if pd.isna(raw):
            continue
        art = str(int(raw)) if isinstance(raw, float) and float(raw).is_integer() else str(raw).strip()
        try:
            new_price = round(float(str(r[cols[2]]).replace(",", ".")), 2)
        except Exception:
            continue
        item = db.query(Inventory).filter(Inventory.part_number_clean == clean_part_number(art)).first()
        if not item:
            missing.append(art)
            continue
        old = round(item.price_3pl_emex or 0, 2)
        if abs(old - new_price) < 0.005:
            continue
        rows.append({"art": item.part_number, "old": old, "new": new_price,
                     "diff": round((new_price / old - 1) * 100, 2) if old else 0})
        if apply == "1":
            item.price_3pl_emex = new_price
            item.markup_mode = "manual"
    if apply == "1":
        db.commit()
    return {"ok": True, "applied": apply == "1", "rows": rows, "missing": missing}


# ---------------- сохранить расходы ----------------
@router.post("/emex/save-settings")
def emex_save_settings(payload: dict = Body(...), db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    for k in DEFAULTS:
        if k in payload:
            set_setting(db, k, str(payload[k]))
    return {"ok": True}


@router.get("/emex/orders")
def emex_orders(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """История заказов: группируем продажи по номеру+дате заказа Emex."""
    import re
    inv = {i.part_number_clean: i for i in db.query(Inventory).all()}
    grouped = {}
    q = (db.query(StockTransaction)
           .filter(StockTransaction.tx_type == "sale")
           .order_by(StockTransaction.op_date.asc(), StockTransaction.id.asc()))
    for s_ in q.all():
        d_ = s_.op_date or (s_.created_at.date() if s_.created_at else None)
        m = re.search(r"№\s*(\d+)", s_.notes or "")
        num = m.group(1) if m else ""
        date_str = d_.strftime("%d.%m.%Y") if d_ else "—"
        key = (num, date_str)
        it = inv.get(s_.part_number_clean)
        grouped.setdefault(key, []).append({
            "art": it.part_number if it else s_.part_number_clean,
            "type": (it.description if it else ""),
            "qty": abs(s_.quantity or 0), "price": round(s_.price or 0, 2),
            "cost": round(s_.cost_at_sale or 0, 2),
        })
    out = []
    for (num, date_str), lines in grouped.items():
        title = (f"Заказ №{num} от {date_str}" if num else f"Заказ от {date_str}")
        out.append({"num": num, "date": date_str, "title": title, "lines": lines,
                    "units": sum(l["qty"] for l in lines),
                    "sum": round(sum(l["qty"] * l["price"] for l in lines), 2)})
    return {"orders": out}


@router.get("/emex/orders-export")
def emex_orders_export(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Экспорт истории заказов в Excel."""
    data = emex_orders(db=db, user=user)["orders"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "История заказов"
    head = ["Заказ №", "Дата", "Артикул", "Наименование", "Кол-во",
            "Цена с НДС ₽/ед", "Сумма с НДС ₽", "Себест. ₽/ед", "Прибыль ₽"]
    for j, h in enumerate(head, 1):
        c = ws.cell(1, j, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="2B4C7E")
    r = 2
    VAT = 1.22
    STICK = float(get_setting(db, "sticker", "39"))
    if get_setting(db, "sticker_vat", "1") == "1":
        STICK = STICK / VAT
    for o in data:
        for ln in o["lines"]:
            profit = (ln["price"] / VAT - ln["cost"] - STICK) * ln["qty"]
            ws.cell(r, 1, o["num"] or "—")
            ws.cell(r, 2, o["date"])
            ws.cell(r, 3, ln["art"])
            ws.cell(r, 4, ln["type"])
            ws.cell(r, 5, ln["qty"])
            ws.cell(r, 6, ln["price"])
            ws.cell(r, 7, round(ln["qty"] * ln["price"], 2))
            ws.cell(r, 8, ln["cost"])
            ws.cell(r, 9, round(profit, 2))
            for col in "FGHI":
                ws[f"{col}{r}"].number_format = "#,##0.00"
            r += 1
    # итог
    ws.cell(r, 4, "ИТОГО").font = Font(bold=True)
    ws.cell(r, 5, f"=SUM(E2:E{r-1})").font = Font(bold=True)
    ws.cell(r, 7, f"=SUM(G2:G{r-1})").font = Font(bold=True)
    ws.cell(r, 9, f"=SUM(I2:I{r-1})").font = Font(bold=True)
    for col in "GI":
        ws[f"{col}{r}"].number_format = "#,##0.00"
    for col, w in zip("ABCDEFGHI", [10, 12, 14, 40, 8, 15, 15, 14, 14]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"История_заказов_Emex_{datetime.date.today().strftime('%d%m%y')}.xlsx"
    from urllib.parse import quote
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(fname)}"})


# ---------------- ПУБЛИЧНЫЙ прайс по секретной ссылке (без логина) ----------------
@router.get("/price/{token}")
def price_feed(token: str, db: Session = Depends(get_db)):
    token = token.removesuffix(".xlsx").removesuffix(".xls").removesuffix(".csv")
    real = get_setting(db, "price_token", "")
    if not real or token != real:
        return JSONResponse({"error": "not found"}, status_code=404)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Прайс"
    for j, h in enumerate(["№ детали", "Марка", "Цена детали", "Количество, шт.", "Товарная группа"], 1):
        ws.cell(1, j, h).font = Font(bold=True)
    r = 2
    for i in db.query(Inventory).all():
        qty = i.quantity or 0
        price = i.price_3pl_emex or 0
        if qty <= 0 or price <= 0:
            continue
        ws.cell(r, 1, i.part_number)
        ws.cell(r, 2, i.brand or "")
        ws.cell(r, 3, round(price, 2))
        ws.cell(r, 4, qty)
        r += 1
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="price.xlsx"'},
    )
DISPARE_EOF_MARKER
echo "  записан: app/routers/emex.py"
mkdir -p "$(dirname "app/routers/pages.py")"
cat > "app/routers/pages.py" << 'DISPARE_EOF_MARKER'
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import (
    verify_password, create_token, hash_password,
    COOKIE_NAME, clean_part_number,
)
from app.models import User, CatalogItem, OurPrice, Watchlist, Supplier
from app.services.emex_client import get_budget_status

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


def _get_user_or_none(request: Request, db: Session) -> User | None:
    from app.auth import get_current_user
    try:
        return get_current_user(request, db)
    except Exception:
        return None


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Неверный логин или пароль",
        })
    # Постепенная миграция хешей: если пароль верный, но хеш старый (SHA-256),
    # прозрачно пересчитываем его в Argon2id. Пользователь ничего не замечает.
    from app.auth import needs_rehash
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    token = create_token(user.id, user.username)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=30 * 86400, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    stats = {
        "suppliers": db.query(func.count(Supplier.id)).scalar(),
        "catalog_items": db.query(func.count(CatalogItem.id)).scalar(),
        "our_prices": db.query(func.count(OurPrice.id)).scalar(),
        "watchlist": db.query(func.count(Watchlist.id)).filter(Watchlist.active == True).scalar(),
    }
    budget = get_budget_status(db)

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "stats": stats, "budget": budget,
    })


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("search.html", {"request": request, "user": user})


@router.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    suppliers = db.query(Supplier).all()
    return templates.TemplateResponse("catalog.html", {
        "request": request, "user": user, "suppliers": suppliers,
    })


@router.get("/our-prices", response_class=HTMLResponse)
def our_prices_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("our_prices.html", {"request": request, "user": user})


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("watchlist.html", {"request": request, "user": user})


@router.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("inventory.html", {"request": request, "user": user})


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    user = _get_user_or_none(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse("/", status_code=302)

    users = db.query(User).all()
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "users": users,
    })


@router.post("/settings/add-user")
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _get_user_or_none(request, db)
    if not user or not user.is_admin:
        return RedirectResponse("/", status_code=302)

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return RedirectResponse("/settings?error=exists", status_code=302)

    db.add(User(username=username, password_hash=hash_password(password)))
    db.commit()
    return RedirectResponse("/settings?ok=1", status_code=302)
DISPARE_EOF_MARKER
echo "  записан: app/routers/pages.py"
mkdir -p "$(dirname "docker-compose.yml")"
cat > "docker-compose.yml" << 'DISPARE_EOF_MARKER'
services:
  app:
    build: .
    container_name: dispare-app
    restart: unless-stopped
    volumes:
      - ./data:/app/data
    env_file:
      - .env
    expose:
      - "8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health', timeout=5).status==200 else sys.exit(1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  nginx:
    image: nginx:alpine
    container_name: dispare-nginx
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf
    depends_on:
      - app
DISPARE_EOF_MARKER
echo "  записан: docker-compose.yml"
mkdir -p "$(dirname "requirements.txt")"
cat > "requirements.txt" << 'DISPARE_EOF_MARKER'
fastapi==0.115.0
uvicorn[standard]==0.30.0
sqlalchemy==2.0.35
aiosqlite==0.20.0
python-multipart==0.0.12
jinja2==3.1.4
openpyxl==3.1.5
pandas==2.2.3
httpx==0.27.2
python-jose[cryptography]==3.3.0
python-dotenv==1.0.1
apscheduler==3.10.4
xlrd==2.0.1
argon2-cffi==23.1.0
DISPARE_EOF_MARKER
echo "  записан: requirements.txt"

# 4) Проверить, что все python-файлы целые (защита от порчи при вставке)
echo "=== Проверка синтаксиса ==="
BAD=0
for f in app/config.py app/auth.py app/set_password.py app/backup_db.py app/models.py app/main.py app/migrate.py app/routers/emex.py app/routers/pages.py; do
  if python3 -m py_compile "$f" 2>/dev/null; then
    echo "  OK  $f"
  else
    echo "  ОШИБКА в $f — файл повреждён при вставке. ДЕПЛОЙ ОСТАНОВЛЕН."
    python3 -m py_compile "$f"
    BAD=1
  fi
done
if [ "$BAD" = "1" ]; then
  echo ""
  echo "Файлы записаны с ошибкой. Живое приложение НЕ тронуто. Вставьте скрипт заново целиком."
  exit 1
fi

# 5) SECRET_KEY в .env (генерим, если нет)
echo "=== Проверка .env / SECRET_KEY ==="
touch .env
if grep -q "^SECRET_KEY=." .env; then
  echo "  SECRET_KEY уже задан — оставляю как есть."
else
  KEY="$(openssl rand -hex 32 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(32))')"
  # убрать пустую строку SECRET_KEY= если была, затем дописать
  sed -i '/^SECRET_KEY=$/d' .env
  echo "SECRET_KEY=$KEY" >> .env
  echo "  SECRET_KEY сгенерирован и записан в .env"
  echo "  (все текущие сессии разлогинятся — это нормально)"
fi

# 6) Сборка и запуск
echo "=== Сборка контейнера (может занять пару минут) ==="
docker compose build --no-cache app || { echo "Сборка не прошла. Смотрите ошибку выше."; exit 1; }
docker compose up -d || { echo "Запуск не прошёл."; exit 1; }

# 7) Миграция базы (журнал заказов + защита от повтора)
echo "=== Обновление базы ==="
sleep 5
docker compose exec -T app python -m app.migrate || echo "  ВНИМАНИЕ: миграция не прошла, проверьте логи."

# 8) Проверка здоровья
echo "=== Проверка /health ==="
ok=0
for i in $(seq 1 20); do
  sleep 3
  code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/health 2>/dev/null || echo 000)
  [ "$code" = "200" ] && { ok=1; break; }
  echo "  ...ещё не готов (HTTP $code) $i/20"
done

echo ""
if [ "$ok" = "1" ]; then
  echo "======================================================"
  echo " ГОТОВО. Приложение живо, /health = 200."
  echo " Дальше — смена пароля:"
  echo "   docker compose exec app python -m app.set_password"
  echo " Потом удалите строку ADMIN_PASSWORD из .env и:  docker compose up -d"
  echo "======================================================"
else
  echo "ВНИМАНИЕ: /health не ответил 200. Логи приложения:"
  docker compose logs --tail 40 app
  echo ""
  echo "Откат: docker compose down && cp data/backup_before_v4.db data/dispare.db && git checkout -- . ; docker compose up -d"
fi
