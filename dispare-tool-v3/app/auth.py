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
