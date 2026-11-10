from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import jwt
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel

from ecommerce_service.common.config import get_settings


class Principal(BaseModel):
    user_id: str
    role: Literal["user", "staff", "admin"] = "user"


def create_access_token(principal: Principal, expires_hours: int = 24) -> str:
    settings = get_settings()
    payload = principal.model_dump()
    payload["exp"] = datetime.now(UTC) + timedelta(hours=expires_hours)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Principal:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return Principal.model_validate(payload)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        ) from exc


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    return authorization.split(" ", 1)[1]


def current_principal(token: Annotated[str, Depends(bearer_token)]) -> Principal:
    return decode_access_token(token)


def require_roles(*roles: str):
    def dependency(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return principal

    return dependency
