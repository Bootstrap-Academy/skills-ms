
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import Select
from sqlalchemy.engine import make_url

from api.settings import settings


metadata = MetaData()

challenges_course_tasks = Table(
    "challenges_course_tasks",
    metadata,
    Column("task_id", String(36), primary_key=True),
    Column("course_id", String(256), nullable=False),
    Column("section_id", String(256), nullable=True),
    Column("lecture_id", String(256), nullable=True),
)

challenges_subtasks = Table(
    "challenges_subtasks",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("task_id", String(36), nullable=False),
    Column("creator", String(36), nullable=False),
    Column("creation_timestamp", DateTime(timezone=True), nullable=False),
    Column("xp", Integer, nullable=False, default=0),
    Column("coins", Integer, nullable=False, default=0),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("ty", String(64), nullable=False),
    Column("retired", Boolean, nullable=False, default=False),
)

challenges_user_subtasks = Table(
    "challenges_user_subtasks",
    metadata,
    Column("user_id", String(36), primary_key=True),
    Column("subtask_id", String(36), primary_key=True),
    Column("solved_timestamp", DateTime(timezone=True), nullable=True),
    Column("rating", String(32), nullable=True),
    Column("rating_timestamp", DateTime(timezone=True), nullable=True),
    Column("last_attempt_timestamp", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
)

_engine: AsyncEngine | None = None
_sessionmaker: sessionmaker | None = None


def _ensure_engine() -> AsyncEngine | None:
    """Initialise the challenges database engine if configured."""

    url = settings.challenges_database_url
    if not url:
        return None

    global _engine, _sessionmaker
    if _engine is None:
        url_info = make_url(url)
        engine_kwargs: dict[str, Any] = {
            "pool_pre_ping": True,
            "echo": settings.sql_show_statements,
        }
        if url_info.get_backend_name().startswith("sqlite"):
            # SQLite (used in tests) does not support these pooling options.
            engine_kwargs["connect_args"] = {"check_same_thread": False}  # type: ignore[assignment]
        else:
            engine_kwargs.update(
                pool_recycle=settings.pool_recycle,
                pool_size=settings.pool_size,
                max_overflow=settings.max_overflow,
            )

        _engine = create_async_engine(url, **engine_kwargs)
        _sessionmaker = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)

    return _engine


def challenges_configured() -> bool:
    """Return whether a challenges database connection string is configured."""

    return settings.challenges_database_url is not None


@asynccontextmanager
async def challenges_session() -> AsyncIterator[AsyncSession]:
    """
    Provide an AsyncSession for the challenges database.

    The caller must ensure that a connection string is configured.
    """

    if _sessionmaker is None:
        if _ensure_engine() is None:
            raise RuntimeError("Challenges database is not configured")
    assert _sessionmaker is not None

    async with _sessionmaker() as session:
        yield session


async def execute(statement: Any) -> None:
    """Execute a statement on the challenges database and commit."""

    if _sessionmaker is None:
        if _ensure_engine() is None:
            raise RuntimeError("Challenges database is not configured")
    assert _sessionmaker is not None

    async with _sessionmaker() as session:
        await session.execute(statement)
        await session.commit()


async def fetch_all(statement: Select | Any) -> list[Any]:
    """Fetch all rows for the given statement from the challenges database."""

    if _sessionmaker is None:
        if _ensure_engine() is None:
            raise RuntimeError("Challenges database is not configured")
    assert _sessionmaker is not None

    async with _sessionmaker() as session:
        result = await session.execute(statement)
        return list(result)


async def ensure_schema() -> None:
    """
    Create the challenges tables if they do not exist.

    This is primarily intended for tests, where an in-memory database is used.
    """

    engine = _ensure_engine()
    if engine is None:
        return

    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)


async def dispose_engine() -> None:
    """Dispose the current engine (used in tests to reset the state)."""

    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


__all__ = [
    "challenges_course_tasks",
    "challenges_subtasks",
    "challenges_user_subtasks",
    "challenges_configured",
    "challenges_session",
    "dispose_engine",
    "ensure_schema",
    "execute",
    "fetch_all",
]
