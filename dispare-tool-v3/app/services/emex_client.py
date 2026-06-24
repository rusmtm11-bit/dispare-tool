"""
Клиент для Emex API с кэшированием и контролем расхода бюджета.
Заглушка, готовая к подключению реального API.
"""
import httpx
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models import MarketPrice, EmexBudget
from app.config import (
    EMEX_API_KEY, EMEX_COST_PER_REQUEST,
    EMEX_DAILY_BUDGET, EMEX_CACHE_TTL_HOURS,
)


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def get_budget_status(db: Session) -> dict:
    """Текущий расход за сегодня и за месяц."""
    today = _today()
    month_prefix = today[:7]

    today_rec = db.query(EmexBudget).filter(EmexBudget.date == today).first()
    month_recs = db.query(EmexBudget).filter(EmexBudget.date.like(f"{month_prefix}%")).all()

    return {
        "today_requests": today_rec.requests_count if today_rec else 0,
        "today_cost": round(today_rec.cost_rub if today_rec else 0, 2),
        "month_requests": sum(r.requests_count for r in month_recs),
        "month_cost": round(sum(r.cost_rub for r in month_recs), 2),
        "daily_budget": EMEX_DAILY_BUDGET,
        "cost_per_request": EMEX_COST_PER_REQUEST,
    }


def _increment_budget(db: Session, count: int = 1):
    today = _today()
    rec = db.query(EmexBudget).filter(EmexBudget.date == today).first()
    if not rec:
        rec = EmexBudget(date=today, requests_count=0, cost_rub=0)
        db.add(rec)
    rec.requests_count += count
    rec.cost_rub += count * EMEX_COST_PER_REQUEST
    db.commit()


def _can_spend(db: Session, count: int = 1) -> bool:
    budget = get_budget_status(db)
    return (budget["today_cost"] + count * EMEX_COST_PER_REQUEST) <= EMEX_DAILY_BUDGET


def get_cached_prices(db: Session, part_number_clean: str) -> list[dict] | None:
    """Возвращает кэшированные данные, если они свежее TTL."""
    cutoff = datetime.utcnow() - timedelta(hours=EMEX_CACHE_TTL_HOURS)
    rows = (
        db.query(MarketPrice)
        .filter(
            MarketPrice.part_number_clean == part_number_clean,
            MarketPrice.fetched_at >= cutoff,
        )
        .order_by(MarketPrice.price.asc())
        .all()
    )
    if not rows:
        return None
    return [
        {
            "source": r.source,
            "price": r.price,
            "delivery_days": r.delivery_days,
            "quantity": r.quantity,
            "seller": r.seller,
            "rating": r.rating,
            "fetched_at": r.fetched_at.isoformat() if r.fetched_at else "",
        }
        for r in rows
    ]


def get_price_history(db: Session, part_number_clean: str, days: int = 90) -> list[dict]:
    """Вся история цен по артикулу за N дней."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(MarketPrice)
        .filter(
            MarketPrice.part_number_clean == part_number_clean,
            MarketPrice.fetched_at >= cutoff,
        )
        .order_by(MarketPrice.fetched_at.asc())
        .all()
    )
    return [
        {
            "price": r.price,
            "seller": r.seller,
            "fetched_at": r.fetched_at.isoformat() if r.fetched_at else "",
        }
        for r in rows
    ]


async def fetch_emex_prices(db: Session, part_number_clean: str, force: bool = False) -> dict:
    """
    Запрашивает цены из Emex API.
    Если есть свежий кэш и force=False — вернёт кэш без запроса (бесплатно).
    """
    # Проверяем кэш
    if not force:
        cached = get_cached_prices(db, part_number_clean)
        if cached is not None:
            return {"offers": cached, "from_cache": True, "cost": 0}

    # Проверяем бюджет
    if not _can_spend(db):
        return {
            "offers": [],
            "from_cache": False,
            "cost": 0,
            "error": "Дневной бюджет Emex исчерпан",
        }

    # Проверяем настроен ли API-ключ
    if not EMEX_API_KEY:
        return {
            "offers": _generate_demo_data(part_number_clean),
            "from_cache": False,
            "cost": 0,
            "demo": True,
            "error": "API-ключ Emex не настроен. Показаны демо-данные.",
        }

    # === РЕАЛЬНЫЙ ЗАПРОС К EMEX ===
    # TODO: Подставить реальный endpoint и формат вашего API Emex.
    # Пример структуры (адаптировать под документацию Emex):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                "https://api.emex.ru/api/search/search",
                params={
                    "detailNum": part_number_clean,
                    "locationId": 38831,  # Москва, адаптировать
                    "showAll": "true",
                },
                headers={"Authorization": f"Bearer {EMEX_API_KEY}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"offers": [], "from_cache": False, "cost": 0, "error": f"Ошибка Emex API: {e}"}

    # Считаем расход
    _increment_budget(db)

    # Парсим ответ (адаптировать под реальный формат!)
    offers = []
    for item in data.get("searchResult", data.get("offers", [])):
        offer = {
            "source": "emex",
            "price": float(item.get("price", 0)),
            "delivery_days": int(item.get("deliveryDays", item.get("delivery", 0))),
            "quantity": int(item.get("quantity", item.get("available", 0))),
            "seller": item.get("supplierTitle", item.get("seller", "")),
            "rating": float(item.get("supplierRating", item.get("rating", 0))),
        }
        offers.append(offer)

        # Сохраняем в историю
        db.add(MarketPrice(
            part_number_clean=part_number_clean,
            source="emex",
            price=offer["price"],
            delivery_days=offer["delivery_days"],
            quantity=offer["quantity"],
            seller=offer["seller"],
            rating=offer["rating"],
        ))

    db.commit()

    return {
        "offers": sorted(offers, key=lambda x: x["price"]),
        "from_cache": False,
        "cost": EMEX_COST_PER_REQUEST,
    }


def _generate_demo_data(pn: str) -> list[dict]:
    """Демо-данные для тестирования без реального API."""
    import random
    base = random.randint(500, 15000)
    sellers = ["АвтоДок", "Запчасти24", "МоторЛэнд", "PartPro", "АвтоМир"]
    offers = []
    for i in range(random.randint(3, 7)):
        offers.append({
            "source": "emex (демо)",
            "price": round(base * (0.85 + random.random() * 0.35), 2),
            "delivery_days": random.choice([1, 2, 3, 5, 7, 10, 14]),
            "quantity": random.randint(1, 50),
            "seller": random.choice(sellers),
            "rating": round(3.5 + random.random() * 1.5, 1),
        })
    return sorted(offers, key=lambda x: x["price"])
