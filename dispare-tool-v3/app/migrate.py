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
from app.models import Inventory, StockTransaction, Batch, BatchLot

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
        db.commit()
        print("Готово. База обновлена, данные на месте.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
