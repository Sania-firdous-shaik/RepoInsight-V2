from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.session import get_db
from app.models.models import User

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
def current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)) -> User:
    try:
        data = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        user = db.get(User, int(data["sub"]))
    except (JWTError, ValueError, KeyError):
        user = None
    if not user: raise HTTPException(401, "Invalid or expired access token", headers={"WWW-Authenticate":"Bearer"})
    return user
