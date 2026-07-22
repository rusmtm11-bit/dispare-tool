import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    """Обязательная переменная окружения. Если её нет — приложение не стартует
    (лучше явная ошибка, чем тихий запуск с небезопасным значением по умолчанию)."""
    val = os.getenv(name)
    if not val:
        raise RuntimeError(
            f"Не задана переменная окружения {name}. "
            f"Добавьте её в файл .env и перезапустите. "
            f"Для SECRET_KEY можно сгенерировать: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return val


# --- Безопасность: обязательные секреты, без небезопасных значений по умолчанию ---
SECRET_KEY = _require("SECRET_KEY")

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

# Admin.
# ADMIN_PASSWORD нужен ТОЛЬКО для первого создания пользователя admin.
# Если admin уже есть в базе — переменная не используется и её можно удалить из .env.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")  # None, если не задан — это нормально
