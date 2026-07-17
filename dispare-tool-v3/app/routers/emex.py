"""Emex-раздел: секретный прайс по ссылке, обработка заказа, дашборд продаж."""
import os
import secrets
import datetime
from io import BytesIO

from fastapi import APIRouter, Request, Depends, UploadFile, File, Body
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

import openpyxl
from openpyxl.styles import Font, PatternFill

from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import User, Inventory, StockTransaction, EmexSetting, MarketPrice
from app.services import emex_order

router = APIRouter(tags=["emex"])
templates = Jinja2Templates(directory="app/templates")

OUT_DIR = "data/emex_out"
DEFAULTS = {"commission": "6", "insurance": "9", "sorting": "30", "logistics": "0"}


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
    # последняя рыночная цена по каждому артикулу (если собиралась в мониторинге)
    market = {}
    for m in db.query(MarketPrice).order_by(MarketPrice.fetched_at.asc()).all():
        market[m.part_number_clean] = m.price
    catalog = []
    for i in inv:
        catalog.append({
            "art": i.part_number, "clean": i.part_number_clean,
            "type": i.description or i.brand or "",
            "net": round(i.cost_rub or 0, 2),
            "price": round(i.price_3pl_emex or 0, 2),
            "stock": i.quantity or 0,
            "market": round(market[i.part_number_clean], 2) if market.get(i.part_number_clean) else None,
        })
    sales = []
    q = db.query(StockTransaction).filter(StockTransaction.tx_type == "sale").order_by(StockTransaction.created_at.asc())
    for s in q.all():
        item = by_clean.get(s.part_number_clean)
        sales.append({
            "date": (s.created_at.date().isoformat() if s.created_at else ""),
            "art": item.part_number if item else s.part_number_clean,
            "qty": abs(s.quantity or 0),
            "price": round(s.price or 0, 2),
        })
    settings = {k: float(get_setting(db, k, v)) for k, v in DEFAULTS.items()}
    return {"catalog": catalog, "sales": sales, "settings": settings,
            "token": get_or_create_token(db)}


# ---------------- загрузка заказа ----------------
@router.post("/emex/upload")
async def emex_upload(request: Request, file: UploadFile = File(...),
                      db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    content = await file.read()
    try:
        order_date, lines = emex_order.parse_lqld(content)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Не удалось прочитать файл заказа: {e}"}, status_code=400)
    if not lines:
        return JSONResponse({"ok": False, "error": "В файле не найдено строк заказа."}, status_code=400)

    cons = emex_order.consolidate(lines)
    changes, missing = emex_order.apply_order_to_inventory(db, cons, order_date, user.username)

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


# ---------------- сохранить расходы ----------------
@router.post("/emex/save-settings")
def emex_save_settings(payload: dict = Body(...), db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    for k in DEFAULTS:
        if k in payload:
            set_setting(db, k, str(payload[k]))
    return {"ok": True}


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
    for j, h in enumerate(["№ детали", "Марка", "Цена детали", "Количество, шт."], 1):
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
