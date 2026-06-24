"""Получение курсов валют с сайта ЦБ РФ (бесплатно, без ключей)."""
import httpx
from datetime import datetime
from xml.etree import ElementTree
from sqlalchemy.orm import Session
from app.models import ExchangeRate


CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"


async def fetch_cbr_rates() -> dict[str, float]:
    """Получает текущие курсы ЦБ. Возвращает {'USD': 89.5, 'EUR': 97.2, ...}"""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(CBR_URL)
            resp.encoding = "windows-1251"
            tree = ElementTree.fromstring(resp.text)

        rates = {}
        for valute in tree.findall("Valute"):
            code = valute.find("CharCode").text
            nominal = int(valute.find("Nominal").text)
            value = float(valute.find("Value").text.replace(",", "."))
            rates[code] = round(value / nominal, 4)
        return rates
    except Exception:
        return {}


async def update_rates(db: Session) -> dict[str, float]:
    """Обновляет курсы в базе и возвращает их."""
    rates = await fetch_cbr_rates()
    today = datetime.utcnow().strftime("%Y-%m-%d")

    for code, rate in rates.items():
        existing = (
            db.query(ExchangeRate)
            .filter(ExchangeRate.currency == code, ExchangeRate.date == today)
            .first()
        )
        if existing:
            existing.rate_to_rub = rate
        else:
            db.add(ExchangeRate(currency=code, rate_to_rub=rate, date=today))

    db.commit()
    return rates


def get_latest_rates(db: Session) -> dict[str, float]:
    """Последние сохранённые курсы."""
    subq = db.query(ExchangeRate).order_by(ExchangeRate.date.desc()).limit(50).all()
    rates = {}
    for r in subq:
        if r.currency not in rates:
            rates[r.currency] = r.rate_to_rub
    return rates


def convert_to_rub(amount: float, currency: str, rates: dict[str, float]) -> float:
    """Конвертирует сумму в рубли."""
    if currency.upper() == "RUB":
        return amount
    rate = rates.get(currency.upper(), 0)
    return round(amount * rate, 2) if rate else 0
