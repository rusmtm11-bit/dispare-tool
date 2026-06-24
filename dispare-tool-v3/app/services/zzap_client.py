"""
Клиент для ZZap.ru API — агрегатор цен от множества продавцов.
Документация: https://wiki.zzap.ru/
Бесплатный для поиска, нужна регистрация как продавец для API-доступа.
"""
import httpx
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import MarketPrice
from app.config import ZZAP_API_KEY, ZZAP_LOGIN


ZZAP_BASE_URL = "https://api.zzap.pro/webservice/datasharing.asmx"


async def fetch_zzap_prices(db: Session, part_number_clean: str) -> dict:
    """Запрос цен через ZZap API."""
    if not ZZAP_API_KEY or not ZZAP_LOGIN:
        return {
            "offers": [],
            "source": "zzap",
            "error": "ZZap API не настроен. Укажите ZZAP_LOGIN и ZZAP_API_KEY в .env",
        }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Шаг 1: Инициировать поиск
            resp = await client.get(
                f"{ZZAP_BASE_URL}/GetSearchResult",
                params={
                    "login": ZZAP_LOGIN,
                    "password": ZZAP_API_KEY,
                    "code_region": "1",  # Москва
                    "search_text": part_number_clean,
                    "type_request": "1",  # по артикулу
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"offers": [], "source": "zzap", "error": f"Ошибка ZZap API: {e}"}

    # Парсим ответ (адаптировать под реальный формат!)
    offers = []
    items = data if isinstance(data, list) else data.get("table", data.get("items", []))

    for item in items[:30]:
        try:
            offer = {
                "source": "zzap",
                "price": float(item.get("price", 0)),
                "delivery_days": int(item.get("delivery_days", item.get("term", 0))),
                "quantity": int(item.get("quantity", item.get("count", 0))),
                "seller": item.get("firm", item.get("supplier", "")),
                "rating": float(item.get("rating", 0)),
            }
            offers.append(offer)

            # Сохраняем в историю
            db.add(MarketPrice(
                part_number_clean=part_number_clean,
                source="zzap",
                price=offer["price"],
                delivery_days=offer["delivery_days"],
                quantity=offer["quantity"],
                seller=offer["seller"],
                rating=offer["rating"],
            ))
        except (ValueError, TypeError, KeyError):
            continue

    if offers:
        db.commit()

    return {
        "offers": sorted(offers, key=lambda x: x["price"]),
        "source": "zzap",
    }
