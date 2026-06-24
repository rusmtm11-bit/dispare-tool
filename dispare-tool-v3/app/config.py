import os
from dotenv import load_dotenv

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-please")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/dispare.db")

# Emex
EMEX_API_KEY = os.getenv("EMEX_API_KEY", "")
EMEX_COST_PER_REQUEST = float(os.getenv("EMEX_COST_PER_REQUEST", "0.12"))
EMEX_DAILY_BUDGET = float(os.getenv("EMEX_DAILY_BUDGET", "200"))
EMEX_MONTHLY_BUDGET = float(os.getenv("EMEX_MONTHLY_BUDGET", "5000"))
EMEX_CACHE_TTL_HOURS = int(os.getenv("EMEX_CACHE_TTL_HOURS", "24"))

# ZZap
ZZAP_LOGIN = os.getenv("ZZAP_LOGIN", "")
ZZAP_API_KEY = os.getenv("ZZAP_API_KEY", "")

# Admin
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
