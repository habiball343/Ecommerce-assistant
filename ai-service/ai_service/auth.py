from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status

from ai_service.schemas import CurrentUser
from ai_service.settings import get_settings


def decode_token(access_token: str) -> CurrentUser:
    settings = get_settings()
    try:
        payload = jwt.decode(access_token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return CurrentUser.model_validate({**payload, "access_token": access_token})
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录令牌无效或已过期") from exc


def require_roles(*roles: str):
    def dependency(authorization: Annotated[str | None, Header()] = None) -> CurrentUser:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "请先登录")
        current_user = decode_token(authorization.split(" ", 1)[1])
        if current_user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前账号无权访问该接口")
        return current_user

    return dependency


def require_internal_service(
    token: Annotated[str | None, Header(alias="X-Internal-Service-Token")] = None,
) -> None:
    """内部接口只接受客服服务的调用。"""
    if token != get_settings().internal_service_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无效的内部访问令牌")


UserDep = Annotated[CurrentUser, Depends(require_roles("user"))]
AdminDep = Annotated[CurrentUser, Depends(require_roles("admin"))]
InternalDep = Annotated[None, Depends(require_internal_service)]
