"""
Главный роутер: поиск по артикулу / списку → сравнение наших цен vs рынок.
"""
from fastapi import APIRouter, Depends, Form, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import CatalogItem, OurPrice, MarketPrice, Supplier, User
from app.services.emex_client import fetch_emex_prices, get_cached_prices, get_price_history
from app.services.cbr_rates import get_latest_rates, convert_to_rub

router = APIRouter(prefix="/api/search", tags=["search"])


def _build_comparison(db: Session, pn_clean: str, rates: dict) -> dict:
    """Собирает полное сравнение по одному артикулу."""

    # Наша цена
    our = db.query(OurPrice).filter(OurPrice.part_number_clean == pn_clean).first()

    # Закупочные цены поставщиков
    catalog_items = db.query(CatalogItem).filter(CatalogItem.part_number_clean == pn_clean).all()
    suppliers_data = []
    for ci in catalog_items:
        supplier = db.query(Supplier).filter(Supplier.id == ci.supplier_id).first()
        price_rub = convert_to_rub(ci.purchase_price, ci.currency, rates)
        suppliers_data.append({
            "supplier": supplier.name if supplier else f"ID {ci.supplier_id}",
            "part_number": ci.part_number,
            "brand": ci.brand,
            "description": ci.description,
            "price": ci.purchase_price,
            "currency": ci.currency,
            "price_rub": price_rub,
        })

    # Рыночные цены (кэш)
    market = get_cached_prices(db, pn_clean) or []

    # Статистика по рынку
    market_prices = [m["price"] for m in market if m["price"] and m["price"] > 0]
    market_stats = {}
    if market_prices:
        market_stats = {
            "min": min(market_prices),
            "max": max(market_prices),
            "avg": round(sum(market_prices) / len(market_prices), 2),
            "count": len(market_prices),
        }

    # Позиция нашей цены vs рынок
    position = {}
    if our and market_stats:
        for label, price_val in [
            ("order", our.price_order),
            ("3pl", our.price_3pl),
            ("3pl_emex", our.price_3pl_emex),
        ]:
            if price_val and market_stats["avg"]:
                diff_pct = round((price_val - market_stats["avg"]) / market_stats["avg"] * 100, 1)
                position[label] = {
                    "price": price_val,
                    "vs_avg": diff_pct,
                    "vs_min": round((price_val - market_stats["min"]) / market_stats["min"] * 100, 1),
                    "status": "ниже" if diff_pct < -3 else ("выше" if diff_pct > 3 else "в рынке"),
                }

    # История (для графика)
    history = get_price_history(db, pn_clean)

    return {
        "part_number_clean": pn_clean,
        "our_prices": {
            "part_number": our.part_number if our else "",
            "brand": our.brand if our else "",
            "price_order": our.price_order if our else None,
            "price_3pl": our.price_3pl if our else None,
            "price_3pl_emex": our.price_3pl_emex if our else None,
        } if our else None,
        "suppliers": suppliers_data,
        "market": market[:20],  # Топ-20 предложений
        "market_stats": market_stats,
        "position": position,
        "history_points": len(history),
        "history": history[-60:],  # Последние 60 точек для графика
    }


@router.get("/single")
async def search_single(
    q: str = "",
    fetch_market: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(q)
    if not pn_clean:
        return {"error": "Введите артикул"}

    rates = get_latest_rates(db)

    # Если попросили — обновляем рыночные данные
    if fetch_market:
        await fetch_emex_prices(db, pn_clean)

    return _build_comparison(db, pn_clean, rates)


@router.post("/bulk")
async def search_bulk(
    part_numbers: str = Form(""),
    fetch_market: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Массовый поиск: список артикулов через запятую/перевод строки."""
    raw_list = [
        p.strip()
        for p in part_numbers.replace(",", "\n").replace(";", "\n").split("\n")
        if p.strip()
    ]

    rates = get_latest_rates(db)
    results = []

    for pn in raw_list[:200]:  # Лимит 200 за раз
        pn_clean = clean_part_number(pn)
        if not pn_clean:
            continue

        if fetch_market:
            await fetch_emex_prices(db, pn_clean)

        comp = _build_comparison(db, pn_clean, rates)
        comp["query"] = pn
        results.append(comp)

    return {"results": results, "total": len(results)}


@router.post("/bulk-file")
async def search_bulk_file(
    file: UploadFile = File(...),
    fetch_market: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Загрузка файла со списком артикулов (txt/csv/xlsx — первая колонка)."""
    content = await file.read()
    fname = (file.filename or "").lower()

    if fname.endswith((".xlsx", ".xls")):
        import pandas as pd
        from io import BytesIO
        df = pd.read_excel(BytesIO(content), dtype=str, engine="openpyxl")
        parts = df.iloc[:, 0].dropna().tolist()
    else:
        text = content.decode("utf-8", errors="ignore")
        parts = [p.strip() for p in text.replace(",", "\n").replace(";", "\n").split("\n") if p.strip()]

    rates = get_latest_rates(db)
    results = []

    for pn in parts[:200]:
        pn_clean = clean_part_number(pn)
        if not pn_clean:
            continue

        if fetch_market:
            await fetch_emex_prices(db, pn_clean)

        comp = _build_comparison(db, pn_clean, rates)
        comp["query"] = pn
        results.append(comp)

    return {"results": results, "total": len(results)}
