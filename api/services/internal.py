from datetime import timedelta

from httpx import AsyncClient

from api.environment import AUTH_URL, INTERNAL_JWT_TTL
from api.utils.jwt import encode_jwt


def _get_token() -> str:
    return encode_jwt({}, timedelta(seconds=INTERNAL_JWT_TTL))


def _get_client(base_url: str) -> AsyncClient:
    return AsyncClient(base_url=base_url + "/_internal", headers={"Authorization": _get_token()})


def auth_client() -> AsyncClient:
    return _get_client(AUTH_URL)
