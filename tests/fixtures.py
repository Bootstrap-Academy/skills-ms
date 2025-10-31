from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from httpx import AsyncClient
from pytest_mock import MockerFixture
from sqlalchemy.ext.asyncio import create_async_engine

from api.app import app
from api.database import db
from api.database import challenges as challenges_db
from api.settings import settings


@pytest.fixture(autouse=True)
async def database(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "engine", create_async_engine("sqlite+aiosqlite:///:memory:"))
    await db.create_tables()


@pytest.fixture(autouse=True)
async def challenges_database(monkeypatch: MonkeyPatch) -> AsyncIterator[None]:
    test_db = Path("tests-challenges.db").resolve()
    if test_db.exists():
        test_db.unlink()

    monkeypatch.setattr(
        settings,
        "challenges_database_url",
        f"sqlite+aiosqlite:///{test_db.as_posix()}",
        raising=False,
    )
    await challenges_db.dispose_engine()
    await challenges_db.ensure_schema()
    yield
    await challenges_db.dispose_engine()
    if test_db.exists():
        test_db.unlink()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
async def auth_client(client: AsyncClient, mocker: MockerFixture) -> AsyncIterator[AsyncClient]:
    mocker.patch(
        "api.auth.JWTAuth.__call__",
        AsyncMock(
            return_value={
                "uid": "user-1",
                "rt": "refresh-token",
                "data": {"email_verified": True, "admin": False},
            }
        ),
    )
    mocker.patch("api.schemas.user.auth_redis.exists", AsyncMock(return_value=False))
    yield client
