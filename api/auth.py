from typing import Any

from fastapi import Depends, Request
from fastapi.openapi.models import HTTPBearer
from fastapi.security.base import SecurityBase
from pydantic import ValidationError

from .exceptions.auth import EmailNotVerifiedError, InvalidTokenError, PermissionDeniedError
from .schemas.user import User, UserAccessToken
from .utils.jwt import decode_jwt


def get_token(request: Request) -> str:
    authorization: str = request.headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ")


class HTTPAuth(SecurityBase):
    def __init__(self) -> None:
        self.model = HTTPBearer()
        self.scheme_name = self.__class__.__name__

    async def __call__(self, request: Request) -> Any:
        raise NotImplementedError


class JWTAuth(HTTPAuth):
    async def __call__(self, request: Request) -> dict[Any, Any] | None:
        return decode_jwt(get_token(request))


# static_token_auth = Depends(StaticTokenAuth("secret token"))
jwt_auth = Depends(JWTAuth())


@Depends
async def public_auth(data: dict[Any, Any] = jwt_auth) -> User | None:
    try:
        token: UserAccessToken = UserAccessToken.parse_obj(data)
    except (InvalidTokenError, ValidationError):
        return None

    if await token.is_revoked():
        return None

    return token.to_user()


@Depends
async def user_auth(user: User | None = public_auth) -> User:
    if user is None:
        raise InvalidTokenError
    return user


@Depends
async def admin_auth(user: User = user_auth) -> User:
    if not user.admin:
        raise PermissionDeniedError
    return user


@Depends
async def is_admin(user: User | None = public_auth) -> bool:
    return user is not None and user.admin


async def _require_verified_email(user: User = user_auth) -> None:
    if not user.email_verified and not user.admin:
        raise EmailNotVerifiedError


require_verified_email = Depends(_require_verified_email)
