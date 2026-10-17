from typing import Callable, cast
from unittest.mock import AsyncMock, MagicMock

from _pytest.monkeypatch import MonkeyPatch
from redis.asyncio import Redis, from_url
from redis.asyncio.connection import Connection

from ._utils import import_module
from api import redis
from api.settings import settings


async def test__redis(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://my_redis_host:4953/42")

    r = import_module(redis).redis

    assert isinstance(r, Redis)
    assert r.connection_pool.connection_kwargs == {
        "host": "my_redis_host",
        "port": 4953,
        "db": 42,
        "encoding": "utf-8",
        "decode_responses": True,
    }


async def test__close_redis__disconnects_the_pooled_connections_of_both_clients(monkeypatch: MonkeyPatch) -> None:
    connections = []
    for name in ("redis", "auth_redis"):
        client = cast(Callable[..., Redis], from_url)("redis://my_redis_host:4953/42", decode_responses=True)
        # An open connection back in the pool, as `clear_cache` leaves one after the erasure of an account.
        connection = Connection(**client.connection_pool.connection_kwargs)
        connection._reader, connection._writer = MagicMock(), MagicMock(wait_closed=AsyncMock())
        client.connection_pool._available_connections.append(connection)
        monkeypatch.setattr(redis, name, client)
        connections.append((connection, connection._writer))

    await redis.close_redis()

    for connection, writer in connections:
        writer.close.assert_called_once_with()
        writer.wait_closed.assert_awaited_once_with()
        # Nothing is left for `AbstractConnection.__del__` to close once the event loop has ended.
        assert connection._writer is None
