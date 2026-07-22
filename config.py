from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.database import init_db, SessionLocal
from app.auth import ensure_admin
from app.routers import pages, api_catalog, api_prices, api_search, api_market, api_inventory, emex


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    db = SessionLocal()
    ensure_admin(db)
    db.close()

    # Запускаем фоновые задачи (обновление курсов, мониторинг watchlist)
    from app.scheduler import start_scheduler
    scheduler = start_scheduler()

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Dispare Trading — Проценка",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
def health():
    """Проверка живости для Docker healthcheck. Пингует базу."""
    from sqlalchemy import text
    from app.database import engine
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "error", "database": str(e)}, status_code=503)

# Роутеры
app.include_router(pages.router)
app.include_router(api_catalog.router)
app.include_router(api_prices.router)
app.include_router(api_search.router)
app.include_router(api_market.router)
app.include_router(api_inventory.router)
app.include_router(emex.router)
