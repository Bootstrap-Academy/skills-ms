"""Opt-in actual ASGI/SQL admission concurrency; only owned synthetic fixtures.

Run directly, outside pytest's middleware-removing conftest, with
L1_ADMISSION_SYNTHETIC=1 and explicit local DATABASE_URL, AUTH_URL, SHOP_URL,
REDIS_URL, AUTH_REDIS_URL and JWT_SECRET. The database must be fully migrated;
the local backend must recognize the synthetic principal below.
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import patch
from urllib.parse import urlparse
from uuid import uuid4


assert os.environ.get("L1_ADMISSION_SYNTHETIC") == "1", "requires owned synthetic fixtures"
for name in ["DATABASE_URL", "AUTH_URL", "SHOP_URL", "REDIS_URL", "AUTH_REDIS_URL"]:
    assert urlparse(os.environ[name]).hostname in {"127.0.0.1", "localhost", "::1"}, name
assert os.environ["JWT_SECRET"]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from api.app import app  # noqa: E402
from api.database import db, db_context, filter_by  # noqa: E402
from api.database.database import DB  # noqa: E402
from api.endpoints import course as endpoint  # noqa: E402
from api.models import CourseAccess, LastWatch  # noqa: E402
from api.redis import auth_redis, redis  # noqa: E402
from api.schemas.course import Course  # noqa: E402
from api.services.courses import COURSES  # noqa: E402
from api.utils.cache import clear_cache, redis_cached  # noqa: E402
from api.utils.docs import get_example  # noqa: E402
from api.utils.jwt import encode_jwt  # noqa: E402


OWNER = "e3f8a50a-a5a3-444a-9026-77336f716d03"


class PoolStats(Protocol):
    """The inspected SQLAlchemy 1.4 pool counters lack complete type stubs."""

    _max_overflow: int

    def size(self) -> int:
        """Return the configured retained capacity."""
        ...

    def checkedout(self) -> int:
        """Return the currently checked-out connection count."""
        ...


def headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + encode_jwt(
            {"uid": OWNER, "rt": "l1-fix-3-pool", "data": {"admin": False, "email_verified": True}}, timedelta(hours=1)
        )
    }


async def saturation(client: AsyncClient, course: Course, size: int, overflow: int) -> dict[str, Any]:
    assert db.admission_engine is not None
    normal, admission = cast(PoolStats, db.engine.pool), cast(PoolStats, db.admission_engine.pool)
    capacity = size + overflow
    normal_capacity = normal.size() + normal._max_overflow
    assert normal_capacity == capacity - 1
    assert admission.size() == 1 and admission._max_overflow == 0
    ready, release = asyncio.Event(), asyncio.Event()
    arrivals, peak = 0, 0

    def record(*_: Any) -> None:
        nonlocal peak
        peak = max(peak, normal.checkedout() + admission.checkedout())

    for engine in [db.engine, db.admission_engine]:
        event.listen(engine.sync_engine, "checkout", record)
    reader = endpoint.get_owned_courses

    async def held(user_id: str) -> set[str]:
        nonlocal arrivals
        arrivals += 1
        if arrivals == normal_capacity:
            ready.set()
        await release.wait()
        return await reader(user_id)

    count = capacity if capacity == 40 else capacity + 3
    start = asyncio.get_running_loop().time()
    tasks: list[asyncio.Task[Any]] = []
    try:
        with patch.object(endpoint, "get_owned_courses", held):
            tasks = [asyncio.create_task(client.get("/course_access", headers=headers())) for _ in range(count)]
            await asyncio.wait_for(ready.wait(), 10)
            assert normal.checkedout() == normal_capacity
            assert admission.checkedout() == 0
            assert arrivals == normal_capacity and not any(task.done() for task in tasks)
            released_at = asyncio.get_running_loop().time()
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert all(response.status_code == 200 for response in responses), [r.status_code for r in responses]
        assert all(course.id in {row["id"] for row in response.json()} for response in responses)
        assert peak <= capacity
        assert normal.checkedout() == admission.checkedout() == 0
        return {
            "configured": [size, overflow],
            "actual_admission_reserve": admission.size(),
            "requests": count,
            "normal_slots_held_before_release": normal_capacity,
            "queued_requests": count - normal_capacity,
            "statuses": [response.status_code for response in responses],
            "elapsed_seconds": asyncio.get_running_loop().time() - start,
            "after_barrier_release_seconds": asyncio.get_running_loop().time() - released_at,
            "peak_total_checkout": peak,
            "idle_capacity": normal.size() + admission.size(),
        }
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for engine in [db.engine, db.admission_engine]:
            event.remove(engine.sync_engine, "checkout", record)


async def transaction_preservation(course: Course) -> None:
    # A public read neither sees uncommitted grants nor rolls back its caller.
    async with db_context():
        await db.add(CourseAccess(user_id=OWNER, course_id=course.id))
        await db.session.flush()
        assert await db.exists(filter_by(CourseAccess, user_id=OWNER, course_id=course.id))
        assert course.id not in await endpoint.get_owned_courses(OWNER)
        assert await db.exists(filter_by(CourseAccess, user_id=OWNER, course_id=course.id))
        await db.session.rollback()
    assert course.id not in await endpoint.get_owned_courses(OWNER)
    async with db_context():
        await db.add(CourseAccess(user_id=OWNER, course_id=course.id))
    async with db_context():
        row = await db.get(CourseAccess, user_id=OWNER, course_id=course.id)
        assert row is not None
        await db.delete(row)
        await db.session.flush()
        assert not await db.exists(filter_by(CourseAccess, user_id=OWNER, course_id=course.id))
        assert course.id in await endpoint.get_owned_courses(OWNER)
        assert not await db.exists(filter_by(CourseAccess, user_id=OWNER, course_id=course.id))
        await db.session.rollback()
    assert course.id in await endpoint.get_owned_courses(OWNER)


async def cancellation(client: AsyncClient) -> None:
    assert db.admission_engine is not None
    reader = endpoint.get_owned_courses
    entered = asyncio.Event()

    async def marked(user_id: str) -> set[str]:
        entered.set()
        return await reader(user_id)

    # Cancel an actual request that retains its progress-read connection while
    # waiting for the deliberately occupied reserved slot.
    async with AsyncExitStack() as occupied:
        for _ in range(cast(PoolStats, db.admission_engine.pool).size()):
            await occupied.enter_async_context(db.admission_engine.connect())
        with patch.object(endpoint, "get_owned_courses", marked):
            task = asyncio.create_task(client.get("/course_access", headers=headers()))
            await asyncio.wait_for(entered.wait(), 5)
            await asyncio.sleep(0.05)
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
    assert cast(PoolStats, db.engine.pool).checkedout() == cast(PoolStats, db.admission_engine.pool).checkedout() == 0

    # Delay the real ownership SELECT at its database boundary. The actual
    # server executes the delay and original query; no ownership rows are
    # replaced. Cancellation must close/invalidate the in-flight connection.
    executing = asyncio.Event()

    def delay(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> Any:
        if "skills_course_access" in statement:
            executing.set()
            sleep = "pg_sleep(10)" if db.engine.dialect.name == "postgresql" else "SLEEP(10)"
            # Both fragments are test-controlled SQL, with original bound
            # parameters retained; no user-supplied SQL is interpolated.
            statement = (
                f"SELECT original.* FROM ({statement}) AS original CROSS JOIN (SELECT {sleep}) AS delay"  # noqa: S608
            )
        return statement, parameters

    event.listen(db.admission_engine.sync_engine, "before_cursor_execute", delay, retval=True)
    try:
        task = asyncio.create_task(client.get("/course_access", headers=headers()))
        await asyncio.wait_for(executing.wait(), 5)
        await asyncio.sleep(0.05)
        task.cancel()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
    finally:
        event.remove(db.admission_engine.sync_engine, "before_cursor_execute", delay)
    assert cast(PoolStats, db.engine.pool).checkedout() == cast(PoolStats, db.admission_engine.pool).checkedout() == 0
    assert (await client.get("/course_access", headers=headers())).status_code == 200


async def main() -> None:
    initial = (db.engine, db.admission_engine)
    records: list[dict[str, Any]] = []
    course = Course(**{**get_example(Course), "id": str(uuid4()), "price": 123})
    COURSES[course.id] = course
    try:
        async with AsyncClient(
            transport=ASGITransport(app=cast(Any, app)), base_url="http://skills.synthetic"
        ) as client:
            cases = [(20, 20, False), (20, 20, True), (10, 10, False), (2, 0, False), (1, 1, False)]
            for size, overflow, historical in cases:
                source = DB(
                    os.environ["DATABASE_URL"],
                    reserve_admission_connection=True,
                    pool_size=size,
                    max_overflow=overflow,
                    isolation_level="REPEATABLE READ",
                )
                db.engine, db.admission_engine = source.engine, source.admission_engine
                try:
                    if not records:
                        await transaction_preservation(course)
                    if historical:

                        async def old_reader(user_id: str) -> set[str]:
                            return {
                                row.course_id async for row in await db.stream(filter_by(CourseAccess, user_id=user_id))
                            } | {row.course_id async for row in await db.stream(filter_by(LastWatch, user_id=user_id))}

                        old_reader.__name__ = "get_owned_courses"
                        old_reader.__module__ = "api.endpoints.course"
                        await clear_cache("course_access")
                        with patch.object(
                            endpoint, "get_owned_courses", redis_cached("course_access", "user_id")(old_reader)
                        ):
                            records.append(await saturation(client, course, size, overflow))
                    else:
                        records.append(await saturation(client, course, size, overflow))
                        await cancellation(client)
                        records[-1]["cancel_wait_and_actual_query_then_recover"] = True
                    records[-1]["historical_reader_control"] = historical
                finally:
                    await source.dispose()
    finally:
        db.engine, db.admission_engine = initial
        await db.dispose()
        await redis.aclose()
        await auth_redis.aclose()
    print(
        json.dumps(
            {
                "dialect": db.engine.dialect.name,
                "actual_ASGI_JWT": True,
                "transactions_preserved": True,
                "cases": records,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
