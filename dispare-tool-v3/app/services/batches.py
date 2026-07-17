"""Партии закупки: приход товара, сложение остатков, себестоимость.

Метод учёта — СРЕДНЯЯ ВЗВЕШЕННАЯ (как по умолчанию в 1С):
при приходе новой партии себестоимость единицы усредняется по факту:

    новая себест. = (остаток × старая себест. + приход × себест. партии)
                    ─────────────────────────────────────────────────────
                              остаток + приход

Важно: усредняется с ФАКТИЧЕСКИМ остатком (то, что реально лежит после
продаж), а не с начальным количеством партии.

Задел под ФИФО: каждая партия пишется в batch_lots (сколько приехало,
по какой цене, сколько осталось). Продажи списывают лоты по ФИФО.
Поэтому позже можно включить ФИФО-учёт, не потеряв историю.

Прошлые продажи при этом НЕ пересчитываются: в каждой продаже лежит
снимок себестоимости на момент сделки (cost_at_sale).
"""
import datetime
from io import BytesIO

import pandas as pd
from sqlalchemy.orm import Session

from app.models import Inventory, Batch, BatchLot
from app.auth import clean_part_number


def _f(v, default=0.0):
    try:
        s = str(v).replace(",", ".").replace(" ", "").strip()
        if not s or s.lower() == "nan":
            return default
        return float(s)
    except Exception:
        return default


def parse_batch_file(content: bytes):
    """Читает файл партии.

    Ожидаемые колонки (по порядку):
      1 артикул | 2 бренд | 3 наименование | 4 кол-во | 5 себестоимость ₽/ед (чистая, без НДС)
      6 цена Emex ₽ (необязательно)
    """
    if content[:2] == b"PK":
        df = pd.read_excel(BytesIO(content), header=0, dtype=str, engine="openpyxl")
    else:
        df = pd.read_excel(BytesIO(content), header=0, dtype=str, engine="xlrd")
    df = df.dropna(how="all").reset_index(drop=True)
    rows = []
    for _, r in df.iterrows():
        pn = str(r.iloc[0]).strip()
        if not pn or pn.lower() == "nan":
            continue
        rows.append({
            "art": pn,
            "clean": clean_part_number(pn),
            "brand": (str(r.iloc[1]).strip() if len(r) > 1 and str(r.iloc[1]).strip().lower() != "nan" else ""),
            "desc": (str(r.iloc[2]).strip() if len(r) > 2 and str(r.iloc[2]).strip().lower() != "nan" else ""),
            "qty": int(_f(r.iloc[3])) if len(r) > 3 else 0,
            "cost": _f(r.iloc[4]) if len(r) > 4 else 0.0,
            "price": _f(r.iloc[5]) if len(r) > 5 else 0.0,
        })
    return rows


def receive_batch(db: Session, rows, name: str, arrival: datetime.date,
                  start_sale: datetime.date, note: str = ""):
    """Проводит приход партии. Возвращает отчёт по каждой позиции."""
    batch = Batch(name=name, arrival_date=arrival, start_sale_date=start_sale, note=note)
    db.add(batch)
    db.flush()

    report = []
    for r in rows:
        if r["qty"] <= 0:
            continue
        item = db.query(Inventory).filter(Inventory.part_number_clean == r["clean"]).first()
        if item:
            old_qty = item.quantity or 0
            old_cost = item.cost_rub or 0
            new_qty = old_qty + r["qty"]
            # средняя взвешенная по ФАКТИЧЕСКОМУ остатку
            if new_qty > 0:
                new_cost = round((old_qty * old_cost + r["qty"] * r["cost"]) / new_qty, 2)
            else:
                new_cost = r["cost"]
            item.quantity = new_qty
            item.cost_rub = new_cost
            item.other_costs = new_cost          # держим сумму расходов = себестоимости (закупка=0)
            if r["price"] > 0:
                item.price_3pl_emex = r["price"]
            if r["brand"]:
                item.brand = r["brand"]
            if r["desc"]:
                item.description = r["desc"]
            # дата первого появления НЕ сбрасывается — иначе оборачиваемость соврёт
            if not item.first_stock_date:
                item.first_stock_date = arrival
            report.append({"art": r["art"], "new": False, "old_qty": old_qty,
                           "in_qty": r["qty"], "qty": new_qty,
                           "old_cost": round(old_cost, 2), "in_cost": round(r["cost"], 2),
                           "cost": new_cost})
        else:
            item = Inventory(
                part_number=r["art"], part_number_clean=r["clean"],
                brand=r["brand"] or "TOYOTA", description=r["desc"],
                quantity=r["qty"], purchase_price=0, purchase_currency="AED",
                other_costs=r["cost"], cost_rub=r["cost"],
                markup_mode="manual", price_3pl_emex=r["price"] or 0,
                first_stock_date=arrival,
            )
            db.add(item)
            report.append({"art": r["art"], "new": True, "old_qty": 0,
                           "in_qty": r["qty"], "qty": r["qty"],
                           "old_cost": 0, "in_cost": round(r["cost"], 2),
                           "cost": round(r["cost"], 2)})

        db.add(BatchLot(batch_id=batch.id, part_number_clean=r["clean"],
                        qty_in=r["qty"], qty_left=r["qty"],
                        cost_rub=r["cost"], received_at=arrival))
    db.commit()
    return batch, report
