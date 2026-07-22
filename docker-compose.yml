"""Безопасная смена пароля пользователя (по умолчанию — admin).

Запуск на сервере:
    docker compose exec app python -m app.set_password

Пароль вводится в терминале и НЕ отображается на экране, не пишется в файлы,
не попадает в логи. В базе хранится только необратимый Argon2id-хеш.

Указать другого пользователя:
    docker compose exec app python -m app.set_password --user someuser
"""
import sys
import getpass

from app.database import SessionLocal, init_db
from app.models import User
from app.auth import hash_password


def main():
    username = "admin"
    if "--user" in sys.argv:
        i = sys.argv.index("--user")
        if i + 1 < len(sys.argv):
            username = sys.argv[i + 1]

    init_db()
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"Пользователь '{username}' не найден в базе.")
            sys.exit(1)

        pw1 = getpass.getpass("Новый пароль: ")
        if len(pw1) < 8:
            print("Слишком короткий пароль. Минимум 8 символов.")
            sys.exit(1)
        pw2 = getpass.getpass("Повторите пароль: ")
        if pw1 != pw2:
            print("Пароли не совпадают.")
            sys.exit(1)

        user.password_hash = hash_password(pw1)
        db.commit()
        print(f"Пароль пользователя '{username}' изменён. Хеш обновлён на Argon2id.")
        print("Теперь можно удалить ADMIN_PASSWORD из .env — он больше не нужен.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
