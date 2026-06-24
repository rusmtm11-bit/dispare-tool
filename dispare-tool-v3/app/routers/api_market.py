from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import Watchlist, User
from app.services.emex_client import fetch_emex_prices, get_budget_status

router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/budget")
def budget_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return get_budget_status(db)


@router.post("/fetch")
async def fetch_single(
    part_number: str = Form(...),
    force: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(part_number)
    result = await fetch_emex_prices(db, pn_clean, force=force)
    return result


# === Watchlist ===

@router.get("/watchlist")
def list_watchlist(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    items = db.query(Watchlist).order_by(Watchlist.created_at.desc()).all()
    return [
        {
            "id": w.id,
            "part_number": w.part_number,
            "brand": w.brand,
            "frequency": w.frequency,
            "active": w.active,
            "last_checked": w.last_checked.isoformat() if w.last_checked else None,
        }
        for w in items
    ]


@router.post("/watchlist")
def add_to_watchlist(
    part_number: str = Form(...),
    brand: str = Form(""),
    frequency: str = Form("daily"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(part_number)
    existing = db.query(Watchlist).filter(Watchlist.part_number_clean == pn_clean).first()
    if existing:
        existing.active = True
        existing.frequency = frequency
        db.commit()
        return {"ok": True, "message": "Обновлено"}

    db.add(Watchlist(
        part_number=part_number, part_number_clean=pn_clean,
        brand=brand, frequency=frequency,
    ))
    db.commit()
    return {"ok": True, "message": "Добавлено в мониторинг"}


@router.post("/watchlist/bulk")
def add_bulk_watchlist(
    part_numbers: str = Form(...),
    frequency: str = Form("daily"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    raw = [p.strip() for p in part_numbers.replace(",", "\n").replace(";", "\n").split("\n") if p.strip()]
    added = 0
    for pn in raw[:1000]:
        pn_clean = clean_part_number(pn)
        if not pn_clean:
            continue
        existing = db.query(Watchlist).filter(Watchlist.part_number_clean == pn_clean).first()
        if not existing:
            db.add(Watchlist(part_number=pn, part_number_clean=pn_clean, frequency=frequency))
            added += 1
    db.commit()
    return {"added": added, "total_input": len(raw)}


@router.delete("/watchlist/{item_id}")
def remove_from_watchlist(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    return {"ok": True}
