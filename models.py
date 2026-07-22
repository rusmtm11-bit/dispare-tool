"""Безопасная резервная копия SQLite-базы.

Использует официальный backup API SQLite (корректно при включённом WAL —
простой cp может скопировать базу в несогласованном состоянии).

Запуск на сервере:
    docker compose exec app python -m app.backup_db

Копии складываются в data/backups/ (это смонтированный на хост том, т.е.
они переживают пересборку контейнера). Хранятся последние 30 копий.
Рекомендуется дополнительно копировать эту папку на внешний сервер/облако.
"""
import os
import glob
import sqlite3
import datetime

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "dispare.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
KEEP = 30


def main():
    if not os.path.exists(DB_PATH):
        print(f"База не найдена: {DB_PATH}")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = os.path.join(BACKUP_DIR, f"dispare_{stamp}.db")

    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()

    size_kb = os.path.getsize(dest_path) / 1024
    print(f"Копия создана: {dest_path} ({size_kb:.0f} КБ)")

    # Чистим старые копии, оставляем последние KEEP
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "dispare_*.db")))
    for old in files[:-KEEP]:
        os.remove(old)
        print(f"Удалена старая копия: {os.path.basename(old)}")


if __name__ == "__main__":
    main()
