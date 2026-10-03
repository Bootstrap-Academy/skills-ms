from datetime import timedelta
from enum import Enum
from functools import lru_cache
from ssl import SSLContext

from httpx import AsyncClient, Response, create_ssl_context

from api.logger import get_logger
from api.settings import settings
from api.utils.jwt import encode_jwt


logger = get_logger(__name__)


@lru_cache(maxsize=1)
def client_ssl_context() -> SSLContext:
    # Loading the same trust store for every internal HTTP client dominates
    # short local admission calls. Keep verification and environment trust
    # defaults, while loading trust once per process. Tokens remain per request.
    return create_ssl_context()


class InternalServiceError(Exception):
    pass


class InternalService(Enum):
    AUTH = settings.auth_url
    SHOP = settings.shop_url
    CHALLENGES = settings.challenges_url

    def _get_token(self) -> str:
        audience = self.name.lower()
        return encode_jwt(
            {"aud": audience},
            timedelta(seconds=settings.internal_jwt_ttl),
            secret=settings.internal_jwt_secret(audience),
        )

    @classmethod
    async def _handle_error(cls, response: Response) -> None:
        if response.status_code in [401, 403] or response.status_code in range(500, 600):
            await response.aread()
            raise InternalServiceError(response, response.text)

    @property
    def client(self) -> AsyncClient:
        token = self._get_token()
        return AsyncClient(
            base_url=self.value.rstrip("/") + "/_internal",
            headers={"Authorization": f"Bearer {token}" if self is InternalService.CHALLENGES else token},
            event_hooks={"response": [self._handle_error]},
            verify=client_ssl_context(),
        )
