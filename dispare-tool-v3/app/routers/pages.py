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
