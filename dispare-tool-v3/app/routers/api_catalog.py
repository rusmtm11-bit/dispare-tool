from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.auth import get_current_user, clean_part_number
from app.models import Supplier, CatalogItem, User
from app.services.excel_parser import parse_supplier_excel

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    suppliers = db.query(Supplier).all()
    result = []
    for s in suppliers:
        count = db.query(func.count(CatalogItem.id)).filter(CatalogItem.supplier_id == s.id).scalar()
        result.append({
            "id": s.id, "name": s.name, "currency": s.currency,
            "notes": s.notes, "items_count": count,
        })
    return result


@router.post("/suppliers")
def add_supplier(
    name: str = Form(...), currency: str = Form("USD"), notes: str = Form(""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    existing = db.query(Supplier).filter(Supplier.name == name).first()
    if existing:
        raise HTTPException(400, "Поставщик с таким именем уже существует")
    s = Supplier(name=name, currency=currency, notes=notes)
    db.add(s)
    db.commit()
    return {"id": s.id, "name": s.name}


@router.post("/upload")
async def upload_catalog(
    file: UploadFile = File(...),
    supplier_id: int = Form(...),
    currency: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        raise HTTPException(404, "Поставщик не найден")

    content = await file.read()
    result = parse_supplier_excel(content, file.filename or "")

    if result["errors"] and not result["rows"]:
        raise HTTPException(400, "; ".join(result["errors"]))

    cur = currency or supplier.currency

    added, updated = 0, 0
    for row in result["rows"]:
        pn_clean = clean_part_number(row["part_number"])
        if not pn_clean:
            continue

        existing = (
            db.query(CatalogItem)
            .filter(CatalogItem.supplier_id == supplier_id, CatalogItem.part_number_clean == pn_clean)
            .first()
        )
        if existing:
            existing.part_number = row["part_number"]
            existing.brand = row["brand"] or existing.brand
            existing.description = row["description"] or existing.description
            existing.purchase_price = row["price"] if row["price"] else existing.purchase_price
            existing.currency = cur
            updated += 1
        else:
            db.add(CatalogItem(
                supplier_id=supplier_id,
                part_number=row["part_number"],
                part_number_clean=pn_clean,
                brand=row["brand"],
                description=row["description"],
                purchase_price=row["price"],
                currency=cur,
            ))
            added += 1

    db.commit()
    return {
        "added": added,
        "updated": updated,
        "total_parsed": result["total"],
        "columns_detected": result["columns_detected"],
        "errors": result["errors"],
    }


@router.get("/search")
def search_catalog(
    q: str = "",
    supplier_id: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    pn_clean = clean_part_number(q)
    query = db.query(CatalogItem)
    if supplier_id:
        query = query.filter(CatalogItem.supplier_id == supplier_id)
    if pn_clean:
        query = query.filter(CatalogItem.part_number_clean.contains(pn_clean))

    items = query.limit(limit).all()
    return [
        {
            "id": i.id,
            "part_number": i.part_number,
            "brand": i.brand,
            "description": i.description,
            "purchase_price": i.purchase_price,
            "currency": i.currency,
            "supplier_id": i.supplier_id,
        }
        for i in items
    ]
