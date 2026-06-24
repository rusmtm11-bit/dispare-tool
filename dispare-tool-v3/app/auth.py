import re
import hashlib
import secrets
from datetime import datetime, timedelta
from jose import jwt
from fastapi import Request, HTTPException, Depends
from sqlalchemy.orm import Session
from app.config import SECRET_KEY
from app.database import get_db
from app.models import User

ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30
COOKIE_NAME = "dispare_token"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"


def verify_password(plain: str, hashed: str) -> bool:
    parts = hashed.split("$")
    if len(parts) != 2:
        return False
    salt, h = parts
    return hashlib.sha256((salt + plain).encode()).hexdigest() == h


def create_token(user_id: int, username: str) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = db.query(User).filter(User.id == int(payload["sub"])).first()
        if not user:
            raise HTTPException(status_code=401)
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def clean_part_number(pn: str) -> str:
    return re.sub(r"[\s\-\.\/]", "", pn).upper()


def ensure_admin(db: Session):
    from app.config import ADMIN_USERNAME, ADMIN_PASSWORD
    existing = db.query(User).filter(User.username == ADMIN_USERNAME).first()
    if not existing:
        admin = User(
            username=ADMIN_USERNAME,
            password_hash=hash_password(ADMIN_PASSWORD),
            is_admin=True,
        )
        db.add(admin)
        db.commit()
