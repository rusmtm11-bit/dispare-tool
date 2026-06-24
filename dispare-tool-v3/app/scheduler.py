"""
Фоновые задачи по расписанию:
- Обновление курсов ЦБ (раз в день)
- Обновление рыночных цен по watchlist (раз в день / дважды в день)
"""
import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from app.database import SessionLocal
from app.models import Watchlist

logger = logging.getLogger("scheduler")


def _run_async(coro):
    """Helper to run async function from sync scheduler."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def job_update_rates():
    """Обновить курсы ЦБ."""
    from app.services.cbr_rates import update_rates
    db = SessionLocal()
    try:
        rates = _run_async(update_rates(db))
        logger.info(f"Курсы обновлены: {len(rates)} валют")
    except Exception as e:
        logger.error(f"Ошибка обновления курсов: {e}")
    finally:
        db.close()


def job_update_watchlist():
    """Обновить рыночные цены по watchlist."""
    from app.services.emex_client import fetch_emex_prices
    db = SessionLocal()
    try:
        items = db.query(Watchlist).filter(Watchlist.active == True).all()
        count = 0
        for item in items:
            try:
                result = _run_async(fetch_emex_prices(db, item.part_number_clean))
                if not result.get("error"):
                    item.last_checked = datetime.utcnow()
                    count += 1
            except Exception as e:
                logger.error(f"Ошибка watchlist {item.part_number}: {e}")
        db.commit()
        logger.info(f"Watchlist обновлён: {count}/{len(items)} позиций")
    except Exception as e:
        logger.error(f"Ошибка обновления watchlist: {e}")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()

    # Курсы — каждый день в 10:00 MSK (07:00 UTC)
    scheduler.add_job(job_update_rates, "cron", hour=7, minute=0, id="update_rates")

    # Watchlist — каждый день в 08:00 и 16:00 MSK
    scheduler.add_job(job_update_watchlist, "cron", hour=5, minute=0, id="watchlist_morning")
    scheduler.add_job(job_update_watchlist, "cron", hour=13, minute=0, id="watchlist_afternoon")

    scheduler.start()
    logger.info("Планировщик запущен")

    # Сразу обновим курсы при старте
    try:
        job_update_rates()
    except Exception:
        pass

    return scheduler
