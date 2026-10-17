from typing import Callable, cast

from redis.asyncio import Redis, from_url

from .logger import get_logger
from .settings import settings

logger = get_logger(__name__)

# global redis connection
logger.debug("initializing redis connection")
redis: Redis = cast(Callable[..., Redis], from_url)(settings.redis_url, encoding="utf-8", decode_responses=True)
auth_redis: Redis = cast(Callable[..., Redis], from_url)(
    settings.auth_redis_url, encoding="utf-8", decode_responses=True
)


async def close_redis() -> None:
    """Close the connections of both clients while the event loop still runs.

    A one-shot command calls this before `asyncio.run` returns. Otherwise the garbage collector closes the
    connections after the loop has ended, and `AbstractConnection.__del__` prints
    "RuntimeError: Event loop is closed".
    """

    try:
        await redis.aclose()
    finally:
        await auth_redis.aclose()
