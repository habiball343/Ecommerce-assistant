"""身份认证：校验电商服务签发的 JWT，并按角色限制接口访问。"""

from datetime import timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status

from customer_service.schemas import CurrentUser
from customer_service.settings import get_settings
from customer_service.utils import utcnow


def decode_token(access_token: str) -> CurrentUser:
    settings = get_settings()
    try:
        payload = jwt.decode(access_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return CurrentUser.model_validate(payload)
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录令牌无效或已过期") from exc


def encode_user_token(user_id: str, expires_minutes: int = 10) -> str:
    """以用户身份签发短期令牌：AI Service 用它向电商服务查询，只能读到该用户自己的数据。"""
    settings = get_settings()
    payload = {"user_id": user_id, "role": "user", "exp": utcnow() + timedelta(minutes=expires_minutes)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def require_roles(*roles: str):
    def dependency(authorization: Annotated[str | None, Header()] = None) -> CurrentUser:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
        current_user = decode_token(authorization.split(" ", 1)[1])
        if current_user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账号无权访问该接口")
        return current_user

    return dependency


UserDep = Annotated[CurrentUser, Depends(require_roles("user"))]
StaffDep = Annotated[CurrentUser, Depends(require_roles("staff"))]
AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]
