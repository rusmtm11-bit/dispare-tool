"""Управление складом: остатки, цены, движения, экспорт в Excel."""
from io import BytesIO
from fastapi import APIRouter, Depends, Form, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import Inventory, StockTransaction, User
from app.services.cbr_rates import get_latest_rates, convert_to_rub, update_rates

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def _recalc_prices(item: Inventory, rate: float):
    """Пересчитать продажные цены.
    markup_mode: 'pct' = наценка в %, 'rub' = наценка в рублях, 'manual' = ручная цена.
    """
    base_rub = item.purchase_price * rate if rate else 0
    mode = item.markup_mode or "pct"

    if mode == "pct":
        item.price_order = round(base_rub * (1 + item.markup_order_pct / 100), 2)
        item.price_3pl = round(base_rub * (1 + item.markup_3pl_pct / 100), 2)
        item.price_3pl_emex = round(base_rub * (1 + item.markup_3pl_emex_pct / 100), 2)
    elif mode == "rub":
        item.price_order = round(base_rub + item.markup_order_rub, 2)
        item.price_3pl = round(base_rub + item.markup_3pl_rub, 2)
        item.price_3pl_emex = round(base_rub + item.markup_3pl_emex_rub, 2)
    # mode == "manual" — цены не пересчитываются, оставляем как есть

    item.cost_rub = round(base_rub, 2)
    item.last_rate_used = rate


@router.get("/")
def list_inventory(
    q: str = "", limit: int = 200,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(Inventory)
    if q:
        pn = clean_part_number(q)
        query = query.filter(Inventory.part_number_clean.contains(pn))
    items = query.order_by(Inventory.part_number).limit(limit).all()
    rates = get_latest_rates(db)
    return [
        {
            "id": i.id,
            "part_number": i.part_number,
            "brand": i.brand,
            "description": i.description,
            "quantity": i.quantity,
            "purchase_price": i.purchase_price,
            "purchase_currency": i.purchase_currency,
            "markup_mode": i.markup_mode or "pct",
            "markup_order_pct": i.markup_order_pct,
            "markup_3pl_pct": i.markup_3pl_pct,
            "markup_3pl_emex_pct": i.markup_3pl_emex_pct,
            "markup_order_rub": i.markup_order_rub,
            "markup_3pl_rub": i.markup_3pl_rub,
            "markup_3pl_emex_rub": i.markup_3pl_emex_rub,
            "cost_rub": i.cost_rub,
            "price_order": i.price_order,
            "price_3pl": i.price_3pl,
            "price_3pl_emex": i.price_3pl_emex,
            "margin_order": round(i.price_order - (i.cost_rub or 0), 2) if i.price_order and i.cost_rub else 0,
            "margin_order_pct": round((i.price_order / i.cost_rub - 1) * 100, 1) if i.price_order and i.cost_rub and i.cost_rub > 0 else 0,
            "last_rate_used": i.last_rate_used,
            "updated_at": i.updated_at.isoformat() if i.updated_at else "",
        }
        for i in items
    ]


@router.get("/summary")
def inventory_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    total_sku = db.query(func.count(Inventory.id)).scalar()
    total_qty = db.query(func.sum(Inventory.quantity)).scalar() or 0
    total_value = db.query(func.sum(Inventory.purchase_price * Inventory.quantity)).scalar() or 0
    total_revenue = db.query(func.sum(Inventory.price_order * Inventory.quantity)).scalar() or 0
    rates = get_latest_rates(db)
    usd_rate = rates.get("USD", 0)
    return {
        "total_sku": total_sku,
        "total_quantity": total_qty,
        "total_value_usd": round(total_value, 2),
        "total_value_rub": round(total_value * usd_rate, 2) if usd_rate else 0,
        "total_revenue_potential": round(total_revenue, 2),
        "usd_rate": usd_rate,
    }


@router.post("/update-rates")
async def update_exchange_rates(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Обновить курсы ЦБ вручную с кнопки."""
    rates = await update_rates(db)
    usd = rates.get("USD", 0)
    eur = rates.get("EUR", 0)
    if not usd:
        return {"error": "Не удалось загрузить курсы ЦБ", "usd": 0, "eur": 0}
    return {"ok": True, "usd": usd, "eur": eur, "total_currencies": len(rates)}


@router.post("/add")
def add_item(
    part_number: str = Form(...),
    brand: str = Form(""),
    description: str = Form(""),
    quantity: int = Form(0),
    purchase_price: float = Form(0),
    purchase_currency: str = Form("USD"),
    markup_mode: str = Form("pct"),
    markup_order_pct: float = Form(30),
    markup_3pl_pct: float = Form(35),
    markup_3pl_emex_pct: float = Form(40),
    markup_order_rub: float = Form(0),
    markup_3pl_rub: float = Form(0),
    markup_3pl_emex_rub: float = Form(0),
    price_order_manual: float = Form(0),
    price_3pl_manual: float = Form(0),
    price_3pl_emex_manual: float = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(part_number)
    rates = get_latest_rates(db)
    rate = rates.get(purchase_currency.upper(), rates.get("USD", 0))

    existing = db.query(Inventory).filter(Inventory.part_number_clean == pn_clean).first()
    if existing:
        existing.brand = brand or existing.brand
        existing.description = description or existing.description
        existing.quantity = quantity
        existing.purchase_price = purchase_price
        existing.purchase_currency = purchase_currency
        existing.markup_mode = markup_mode
        existing.markup_order_pct = markup_order_pct
        existing.markup_3pl_pct = markup_3pl_pct
        existing.markup_3pl_emex_pct = markup_3pl_emex_pct
        existing.markup_order_rub = markup_order_rub
        existing.markup_3pl_rub = markup_3pl_rub
        existing.markup_3pl_emex_rub = markup_3pl_emex_rub
        if markup_mode == "manual":
            existing.price_order = price_order_manual
            existing.price_3pl = price_3pl_manual
            existing.price_3pl_emex = price_3pl_emex_manual
            existing.cost_rub = round(purchase_price * rate, 2) if rate else 0
            existing.last_rate_used = rate
        else:
            _recalc_prices(existing, rate)
        db.commit()
        return {"ok": True, "action": "updated", "id": existing.id}

    item = Inventory(
        part_number=part_number, part_number_clean=pn_clean,
        brand=brand, description=description, quantity=quantity,
        purchase_price=purchase_price, purchase_currency=purchase_currency,
        markup_mode=markup_mode,
        markup_order_pct=markup_order_pct, markup_3pl_pct=markup_3pl_pct,
        markup_3pl_emex_pct=markup_3pl_emex_pct,
        markup_order_rub=markup_order_rub, markup_3pl_rub=markup_3pl_rub,
        markup_3pl_emex_rub=markup_3pl_emex_rub,
    )
    if markup_mode == "manual":
        item.price_order = price_order_manual
        item.price_3pl = price_3pl_manual
        item.price_3pl_emex = price_3pl_emex_manual
        item.cost_rub = round(purchase_price * rate, 2) if rate else 0
        item.last_rate_used = rate
    else:
        _recalc_prices(item, rate)
    db.add(item)
    db.commit()

    if quantity > 0:
        db.add(StockTransaction(
            part_number_clean=pn_clean, tx_type="receipt",
            quantity=quantity, price=purchase_price, username=user.username,
            notes="Первичное поступление",
        ))
        db.commit()

    return {"ok": True, "action": "added", "id": item.id}


@router.post("/upload")
async def upload_inventory(
    file: UploadFile = File(...),
    default_currency: str = Form("USD"),
    default_markup_order: float = Form(30),
    default_markup_3pl: float = Form(35),
    default_markup_3pl_emex: float = Form(40),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    import pandas as pd
    content = await file.read()
    fname = (file.filename or "").lower()
    try:
        if fname.endswith(".csv"):
            df = pd.read_csv(BytesIO(content), dtype=str)
        else:
            df = pd.read_excel(BytesIO(content), dtype=str, engine="openpyxl")
    except Exception as e:
        return {"error": str(e), "added": 0, "updated": 0}

    df = df.dropna(how="all").reset_index(drop=True)
    ncols = len(df.columns)
    rates = get_latest_rates(db)
    rate = rates.get(default_currency.upper(), rates.get("USD", 0))

    added, updated = 0, 0
    for _, row in df.iterrows():
        try:
            pn = str(row.iloc[0]).strip()
            if not pn or pn.lower() == "nan":
                continue
            pn_clean = clean_part_number(pn)
            qty, price, brand, desc = 0, 0.0, "", ""
            if ncols >= 5:
                brand = str(row.iloc[1]).strip() if str(row.iloc[1]).strip().lower() != 'nan' else ""
                desc = str(row.iloc[2]).strip() if str(row.iloc[2]).strip().lower() != 'nan' else ""
                qty = int(float(str(row.iloc[3]).replace(",", ".").replace(" ", "")))
                price = float(str(row.iloc[4]).replace(",", ".").replace(" ", ""))
            elif ncols >= 3:
                qty = int(float(str(row.iloc[1]).replace(",", ".").replace(" ", "")))
                price = float(str(row.iloc[2]).replace(",", ".").replace(" ", ""))
            elif ncols >= 2:
                qty = int(float(str(row.iloc[1]).replace(",", ".").replace(" ", "")))

            existing = db.query(Inventory).filter(Inventory.part_number_clean == pn_clean).first()
            if existing:
                existing.quantity = qty
                if price > 0:
                    existing.purchase_price = price
                if brand:
                    existing.brand = brand
                if desc:
                    existing.description = desc
                _recalc_prices(existing, rate)
                updated += 1
            else:
                item = Inventory(
                    part_number=pn, part_number_clean=pn_clean,
                    brand=brand, description=desc, quantity=qty,
                    purchase_price=price, purchase_currency=default_currency,
                    markup_order_pct=default_markup_order,
                    markup_3pl_pct=default_markup_3pl,
                    markup_3pl_emex_pct=default_markup_3pl_emex,
                )
                _recalc_prices(item, rate)
                db.add(item)
                added += 1
            db.flush()
        except Exception:
            continue

    db.commit()
    return {"added": added, "updated": updated}


@router.post("/transaction")
def record_transaction(
    part_number: str = Form(...),
    tx_type: str = Form(...),
    quantity: int = Form(...),
    price: float = Form(0),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(part_number)
    item = db.query(Inventory).filter(Inventory.part_number_clean == pn_clean).first()
    if not item:
        return {"error": "Артикул не найден на складе"}

    if tx_type == "sale":
        if item.quantity < quantity:
            return {"error": f"Недостаточно: есть {item.quantity} шт."}
        item.quantity -= quantity
        qty_change = -quantity
    elif tx_type == "return":
        item.quantity += quantity
        qty_change = quantity
    elif tx_type == "receipt":
        item.quantity += quantity
        qty_change = quantity
    elif tx_type == "adjust":
        qty_change = quantity - item.quantity
        item.quantity = quantity
    else:
        return {"error": "Неизвестный тип операции"}

    db.add(StockTransaction(
        part_number_clean=pn_clean, tx_type=tx_type,
        quantity=qty_change, price=price, notes=notes,
        username=user.username,
    ))
    db.commit()
    return {"ok": True, "new_quantity": item.quantity}


@router.post("/recalc-prices")
def recalc_all_prices(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rates = get_latest_rates(db)
    items = db.query(Inventory).all()
    count = 0
    for item in items:
        if item.markup_mode == "manual":
            rate = rates.get(item.purchase_currency.upper(), rates.get("USD", 0))
            item.cost_rub = round(item.purchase_price * rate, 2) if rate else 0
            item.last_rate_used = rate
        else:
            rate = rates.get(item.purchase_currency.upper(), rates.get("USD", 0))
            if rate > 0:
                _recalc_prices(item, rate)
        count += 1
    db.commit()
    return {"recalculated": count, "usd_rate": rates.get("USD", 0)}


@router.post("/update-markups")
def update_markups(
    markup_mode: str = Form("pct"),
    markup_order_pct: float = Form(30),
    markup_3pl_pct: float = Form(35),
    markup_3pl_emex_pct: float = Form(40),
    markup_order_rub: float = Form(0),
    markup_3pl_rub: float = Form(0),
    markup_3pl_emex_rub: float = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rates = get_latest_rates(db)
    items = db.query(Inventory).all()
    for item in items:
        item.markup_mode = markup_mode
        item.markup_order_pct = markup_order_pct
        item.markup_3pl_pct = markup_3pl_pct
        item.markup_3pl_emex_pct = markup_3pl_emex_pct
        item.markup_order_rub = markup_order_rub
        item.markup_3pl_rub = markup_3pl_rub
        item.markup_3pl_emex_rub = markup_3pl_emex_rub
        rate = rates.get(item.purchase_currency.upper(), rates.get("USD", 0))
        if rate > 0:
            _recalc_prices(item, rate)
    db.commit()
    return {"updated": len(items)}


@router.post("/delete/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = db.query(Inventory).filter(Inventory.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    return {"ok": True}


@router.get("/history/{part_number}")
def get_history(part_number: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pn_clean = clean_part_number(part_number)
    txs = (
        db.query(StockTransaction)
        .filter(StockTransaction.part_number_clean == pn_clean)
        .order_by(StockTransaction.created_at.desc())
        .limit(100).all()
    )
    return [
        {"id": t.id, "type": t.tx_type, "quantity": t.quantity, "price": t.price,
         "notes": t.notes, "username": t.username,
         "created_at": t.created_at.isoformat() if t.created_at else ""}
        for t in txs
    ]


@router.get("/export")
def export_excel(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    items = db.query(Inventory).order_by(Inventory.part_number).all()
    rates = get_latest_rates(db)
    usd_rate = rates.get("USD", 0)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Остатки"

    headers = [
        "Артикул", "Бренд", "Описание", "Кол-во",
        "Закупка (USD)", "Курс USD", "Себестоимость ₽",
        "Цена заказ ₽", "Маржа заказ ₽",
        "Цена 3PL ₽", "Маржа 3PL ₽",
        "Цена 3PL+Emex ₽", "Маржа 3PL+Emex ₽",
    ]
    header_font = Font(bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill(start_color="2563EB", end_color="2563EB", fill_type="solid")
    thin = Side(style="thin", color="D1D5DB")
    border = Border(bottom=thin)

    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    for row_idx, item in enumerate(items, 2):
        cost = item.cost_rub or 0
        ws.cell(row=row_idx, column=1, value=item.part_number)
        ws.cell(row=row_idx, column=2, value=item.brand)
        ws.cell(row=row_idx, column=3, value=item.description)
        ws.cell(row=row_idx, column=4, value=item.quantity)
        ws.cell(row=row_idx, column=5, value=item.purchase_price)
        ws.cell(row=row_idx, column=6, value=item.last_rate_used or usd_rate)
        ws.cell(row=row_idx, column=7, value=cost)
        ws.cell(row=row_idx, column=8, value=item.price_order)
        ws.cell(row=row_idx, column=9, value=round(item.price_order - cost, 2) if item.price_order else 0)
        ws.cell(row=row_idx, column=10, value=item.price_3pl)
        ws.cell(row=row_idx, column=11, value=round(item.price_3pl - cost, 2) if item.price_3pl else 0)
        ws.cell(row=row_idx, column=12, value=item.price_3pl_emex)
        ws.cell(row=row_idx, column=13, value=round(item.price_3pl_emex - cost, 2) if item.price_3pl_emex else 0)
        for c in range(1, 14):
            ws.cell(row=row_idx, column=c).border = border

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 30)

    total_row = len(items) + 2
    ws.cell(row=total_row, column=3, value="ИТОГО:").font = Font(bold=True)
    ws.cell(row=total_row, column=4, value=sum(i.quantity for i in items)).font = Font(bold=True)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=inventory_export.xlsx"},
    )
