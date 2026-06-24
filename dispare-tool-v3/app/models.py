from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, UniqueConstraint, Index
)
from sqlalchemy.sql import func
from app.database import Base


class Supplier(Base):
    __tablename__ = "suppliers"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    currency = Column(String(10), default="USD")
    notes = Column(Text, default="")
    created_at = Column(DateTime, server_default=func.now())


class CatalogItem(Base):
    """Позиции из прайс-листов поставщиков (до 100k строк)."""
    __tablename__ = "catalog_items"
    id = Column(Integer, primary_key=True)
    supplier_id = Column(Integer, nullable=False)
    part_number = Column(String(100), nullable=False, index=True)
    part_number_clean = Column(String(100), nullable=False, index=True)  # без пробелов/тире
    brand = Column(String(200), default="")
    description = Column(Text, default="")
    purchase_price = Column(Float, default=0)
    currency = Column(String(10), default="USD")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("supplier_id", "part_number_clean", name="uq_supplier_part"),
    )


class OurPrice(Base):
    """Наши продажные цены (3 вида)."""
    __tablename__ = "our_prices"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    price_order = Column(Float, default=0)       # под заказ, без эмекс и склада
    price_3pl = Column(Float, default=0)          # со склада 3PL, без эмекс
    price_3pl_emex = Column(Float, default=0)     # 3PL + эмекс
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MarketPrice(Base):
    """История рыночных цен (Emex и другие источники)."""
    __tablename__ = "market_prices"
    id = Column(Integer, primary_key=True)
    part_number_clean = Column(String(100), nullable=False, index=True)
    source = Column(String(50), nullable=False, default="emex")
    price = Column(Float)
    delivery_days = Column(Integer, default=0)
    quantity = Column(Integer, default=0)
    seller = Column(String(300), default="")
    rating = Column(Float, default=0)
    fetched_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_market_pn_date", "part_number_clean", "fetched_at"),
    )


class Watchlist(Base):
    """Артикулы на автоматическом мониторинге Emex."""
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    frequency = Column(String(20), default="daily")  # daily / twice_daily
    last_checked = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    id = Column(Integer, primary_key=True)
    currency = Column(String(10), nullable=False)
    rate_to_rub = Column(Float, nullable=False)
    date = Column(String(10), nullable=False)
    fetched_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("currency", "date", name="uq_currency_date"),
    )


class EmexBudget(Base):
    __tablename__ = "emex_budget"
    id = Column(Integer, primary_key=True)
    date = Column(String(10), nullable=False, unique=True)
    requests_count = Column(Integer, default=0)
    cost_rub = Column(Float, default=0)


class Inventory(Base):
    """Склад: остатки, закупочная цена в валюте, наценки, авто-цены."""
    __tablename__ = "inventory"
    id = Column(Integer, primary_key=True)
    part_number = Column(String(100), nullable=False)
    part_number_clean = Column(String(100), nullable=False, unique=True, index=True)
    brand = Column(String(200), default="")
    description = Column(Text, default="")
    quantity = Column(Integer, default=0)
    purchase_price = Column(Float, default=0)         # закупочная цена
    purchase_currency = Column(String(10), default="USD")
    # Наценки в процентах для каждого канала
    markup_order_pct = Column(Float, default=30)
    markup_3pl_pct = Column(Float, default=35)
    markup_3pl_emex_pct = Column(Float, default=40)
    # Рассчитанные продажные цены в рублях
    price_order = Column(Float, default=0)
    price_3pl = Column(Float, default=0)
    price_3pl_emex = Column(Float, default=0)
    last_rate_used = Column(Float, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockTransaction(Base):
    """Движение товаров: продажа, возврат, поступление, корректировка."""
    __tablename__ = "stock_transactions"
    id = Column(Integer, primary_key=True)
    part_number_clean = Column(String(100), nullable=False, index=True)
    tx_type = Column(String(20), nullable=False)  # receipt / sale / return / adjust
    quantity = Column(Integer, nullable=False)
    price = Column(Float, default=0)
    notes = Column(Text, default="")
    username = Column(String(100), default="")
    created_at = Column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), nullable=False, unique=True)
    password_hash = Column(String(200), nullable=False)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
