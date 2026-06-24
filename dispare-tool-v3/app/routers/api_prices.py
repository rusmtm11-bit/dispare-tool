from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import OurPrice, User
from app.services.excel_parser import parse_our_prices_excel

router = APIRouter(prefix="/api/prices", tags=["our_prices"])


@router.get("/")
def list_our_prices(
    q: str = "", limit: int = 100,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(OurPrice)
    if q:
        pn_clean = clean_part_number(q)
        query = query.filter(OurPrice.part_number_clean.contains(pn_clean))
    items = query.order_by(OurPrice.part_number).limit(limit).all()
    return [
        {
            "id": i.id,
            "part_number": i.part_number,
            "brand": i.brand,
            "price_order": i.price_order,
            "price_3pl": i.price_3pl,
            "price_3pl_emex": i.price_3pl_emex,
            "updated_at": i.updated_at.isoformat() if i.updated_at else "",
        }
        for i in items
    ]


@router.post("/upload")
async def upload_our_prices(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    content = await file.read()
    result = parse_our_prices_excel(content, file.filename or "")

    if result["errors"] and not result["rows"]:
        raise HTTPException(400, "; ".join(result["errors"]))

    added, updated, skipped = 0, 0, 0
    for row in result["rows"]:
        pn_clean = clean_part_number(row["part_number"])
        if not pn_clean:
            skipped += 1
            continue

        try:
            existing = db.query(OurPrice).filter(OurPrice.part_number_clean == pn_clean).first()
            if existing:
                existing.part_number = row["part_number"]
                existing.price_order = row["price_order"]
                existing.price_3pl = row["price_3pl"]
                existing.price_3pl_emex = row["price_3pl_emex"]
                updated += 1
            else:
                db.add(OurPrice(
                    part_number=row["part_number"],
                    part_number_clean=pn_clean,
                    price_order=row["price_order"],
                    price_3pl=row["price_3pl"],
                    price_3pl_emex=row["price_3pl_emex"],
                ))
                added += 1
            db.flush()
        except Exception:
            db.rollback()
            skipped += 1

    db.commit()
    return {"added": added, "updated": updated, "skipped": skipped, "total_parsed": result["total"]}


@router.post("/single")
def upsert_single_price(
    part_number: str = Form(...),
    brand: str = Form(""),
    price_order: float = Form(0),
    price_3pl: float = Form(0),
    price_3pl_emex: float = Form(0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(part_number)
    existing = db.query(OurPrice).filter(OurPrice.part_number_clean == pn_clean).first()
    if existing:
        existing.part_number = part_number
        existing.brand = brand or existing.brand
        existing.price_order = price_order
        existing.price_3pl = price_3pl
        existing.price_3pl_emex = price_3pl_emex
    else:
        db.add(OurPrice(
            part_number=part_number, part_number_clean=pn_clean, brand=brand,
            price_order=price_order, price_3pl=price_3pl, price_3pl_emex=price_3pl_emex,
        ))
    db.commit()
    return {"ok": True}
