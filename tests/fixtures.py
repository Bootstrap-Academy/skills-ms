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


@pytest.fixture(autouse=True)
async def database(monkeypatch: MonkeyPatch, tmp_path: Path) -> AsyncIterator[None]:
    # Independent connections to one disposable file avoid both separate
    # :memory: databases and accidental sharing of uncommitted transactions.
    url = "sqlite+aiosqlite:///" + str(tmp_path / "skills.sqlite")
    engine = create_async_engine(url)
    admission_engine = create_async_engine(url)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "admission_engine", admission_engine)
    try:
        await db.create_tables()
        yield
    finally:
        try:
            await engine.dispose()
        finally:
            await admission_engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(app=app, base_url="http://test") as client:
        yield client


@pytest.fixture
async def auth_client(client: AsyncClient, mocker: MockerFixture) -> AsyncIterator[AsyncClient]:
    # mocker.patch("api.auth.StaticTokenAuth._check_token", AsyncMock(return_value=True))
    mocker.patch("api.auth.JWTAuth.__call__", AsyncMock(return_value={"foo": "bar"}))
    yield client
