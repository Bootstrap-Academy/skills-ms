"""Native checks of the lesson engine tables against an already-started disposable PostgreSQL.

DATABASE_URL must point to an empty local database created only for this run.
Run: DAILY_TEST_POSTGRES_BIN=/path/to/postgresql/bin nix develop --command \
    python tests/run_daily_limit_postgres.py tests.engine_postgres
"""

import asyncio
import os
from typing import Any
from unittest.mock import AsyncMock, patch

from alembic import command
from alembic.config import Config

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from api import models
from api.database import db, db_context, filter_by
from api.services.user_deletion import delete_user_data
from tests.engine_rows import VIOLATIONS, add_engine_rows, module, revision, submission, task_state


def values(model: Any) -> dict[str, Any]:
    return {column.name: getattr(model, column.name) for column in model.__table__.columns}


async def insert(connection: AsyncConnection, *rows: Any) -> None:
    for row in rows:
        await connection.execute(row.__table__.insert().values(**values(row)))


async def refused(engine: AsyncEngine, rows: list[Any], *, with_module: bool = True) -> bool:
    """Whether PostgreSQL refuses the last row after the others; nothing stays behind."""
    *valid, last = rows
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await insert(connection, *([module()] if with_module else []), *valid)
            try:
                await insert(connection, last)
            except IntegrityError:
                return True
            return False
        finally:
            await transaction.rollback()


async def constraints(engine: AsyncEngine) -> None:
    for name, rows in VIOLATIONS.items():
        assert await refused(engine, rows()), name
    # Foreign keys, which SQLite does not enforce in the unit tests.
    assert await refused(engine, [revision()], with_module=False), "revision of an unknown module"
    assert await refused(engine, [task_state("learner")]), "task state of an unknown revision"
    assert await refused(engine, [revision(), submission("learner", "s-1")]), "submission without its task state"
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await insert(connection, module(), revision(), task_state("learner"), submission("learner", "s-1"))
        try:
            await connection.execute(text("DELETE FROM skills_engine_task_states"))
        except IntegrityError:
            pass
        else:
            raise AssertionError("a task state was removed before its submissions")
        finally:
            await transaction.rollback()


async def race(engine: AsyncEngine, base: list[Any], first: Any, second: Any, count: str) -> None:
    """Two transactions insert competing rows; the second waits for the first and is refused when it commits."""
    async with engine.begin() as connection:
        await insert(connection, *base)
    async with engine.connect() as one, engine.connect() as two:
        await one.begin()
        await two.begin()
        await insert(one, first)
        waiting = asyncio.create_task(insert(two, second))
        await asyncio.sleep(0.3)
        # The unique index holds the second insert until the first transaction ends.
        assert not waiting.done(), "the second insert did not wait"
        await one.commit()
        try:
            await waiting
        except IntegrityError:
            await two.rollback()
        else:
            raise AssertionError("both competing rows were accepted")
    async with engine.connect() as connection:
        assert (await connection.execute(text(count))).scalar_one() == 1, count
    async with engine.begin() as connection:
        for table in ("skills_engine_submissions", "skills_engine_task_states", "skills_engine_revisions"):
            await connection.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        await connection.execute(text("DELETE FROM skills_lesson_modules"))


async def races(engine: AsyncEngine) -> None:
    await race(
        engine,
        [module()],
        revision(),
        revision(number=2),
        "SELECT count(*) FROM skills_engine_revisions WHERE active_for IS NOT NULL",
    )
    await race(
        engine,
        [module(), revision(), task_state("learner")],
        submission("learner", "s-1", booked=False, input_hash="1" * 64),
        submission("learner", "s-2", booked=False, input_hash="2" * 64),
        "SELECT count(*) FROM skills_engine_submissions WHERE state = 'accepted'",
    )
    await race(
        engine,
        [module(), revision(), task_state("learner")],
        submission("learner", "s-1", booked=False),
        submission("learner", "s-2", booked=True),
        "SELECT count(*) FROM skills_engine_submissions",
    )


async def erasure() -> None:
    async with db_context():
        await add_engine_rows("user")
        await add_engine_rows("other_user")
    with patch("api.services.user_deletion.clear_cache", AsyncMock()):
        async with db_context():
            await delete_user_data("user")
    async with db_context():
        for model in (models.EngineSubmission, models.EngineTaskState):
            assert not await db.exists(filter_by(model, user_id="user")), model
            assert await db.exists(filter_by(model, user_id="other_user")), model
        assert await db.exists(filter_by(models.EngineRevision, unit_id="engine-unit"))


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            version = (await connection.execute(text("SHOW server_version"))).scalar_one()
        await constraints(engine)
        await races(engine)
    except DBAPIError as error:
        raise AssertionError(f"unexpected database error: {error}") from error
    finally:
        await engine.dispose()
    await erasure()
    print(f"engine tables on PostgreSQL {version}: {len(VIOLATIONS)} refusals, 4 foreign keys, 3 races, erasure")


if __name__ == "__main__":
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(main())
