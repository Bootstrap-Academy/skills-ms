"""L1 real PostgreSQL/backend tests; opt in only with owned T6_* fixtures."""

import asyncio
import os
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import text

from api.database import Base, db, db_context, filter_by
from api.database.database import DB
from api.models.course_access import CourseAccess
from api.models.purchase import CoursePurchase, PurchaseUser
from api.schemas.course import Course
from api.services import purchases
from api.services.internal import InternalService
from api.services.user_deletion import delete_user_data
from api.utils.docs import get_example
from api.utils.jwt import encode_jwt


FOO = "a8d95e0f-71ae-4c49-995e-695b7c93848c"


@pytest.fixture
async def ledger(mocker: Any) -> Any:
    if not os.getenv("T6_BACKEND_URL"):
        pytest.skip("requires isolated synthetic backend/PostgreSQL")
    source = DB(os.environ["T6_SOURCE_DB"], reserve_admission_connection=True)
    conn = None
    try:
        mocker.patch.object(db, "engine", source.engine)
        mocker.patch.object(db, "admission_engine", source.admission_engine)
        async with source.engine.begin() as schema_conn:
            await schema_conn.run_sync(Base.metadata.drop_all)
            await schema_conn.run_sync(Base.metadata.create_all)
        mocker.patch.object(
            InternalService,
            "client",
            new_callable=property,
            fget=lambda _: AsyncClient(
                base_url=os.environ["T6_BACKEND_URL"],
                headers={
                    "Authorization": encode_jwt(
                        {"aud": "shop"}, timedelta(minutes=10), secret="synthetic-T6-local-test"
                    )
                },
            ),
        )
        mocker.patch("api.services.purchases.get_user_status", AsyncMock(return_value=200))
        mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
        conn = await asyncpg.connect(os.environ["T6_BACKEND_DB"])
        await conn.execute(
            "INSERT INTO coins(user_id,coins,withheld_coins) VALUES($1,100000,0) "
            "ON CONFLICT(user_id) DO UPDATE SET coins=100000",
            UUID(FOO),
        )
        yield conn
    finally:
        try:
            if conn is not None:
                await conn.close()
        finally:
            await source.dispose()


def course() -> Course:
    return Course(**{**get_example(Course), "id": str(uuid4()), "price": 100})


def accept(quote: Any) -> purchases.Acceptance:
    return purchases.Acceptance(
        order_id=quote["offer"]["id"],
        offer_hash=quote["offer"]["hash"],
        accepted=True,
        early_performance_requested=True,
    )


async def quote(c: Course) -> Any:
    async with db_context():
        return await purchases.offer(FOO, c)


async def buy(c: Course, a: purchases.Acceptance) -> Any:
    async with db_context():
        return await purchases.buy(FOO, c, a)


async def test_parallel_distinct_offers_and_completed_order_keep_one_debit(ledger: Any) -> None:
    c = course()
    q1 = await quote(c)
    q2 = await quote(c)
    results = await asyncio.gather(buy(c, accept(q1)), buy(c, accept(q2)), return_exceptions=True)
    assert any(isinstance(r, dict) and r["state"] == "fulfilled" for r in results), results
    async with db_context():
        rows = await db.all(filter_by(CoursePurchase, course_id=c.id))
        assert sum(r.state == "fulfilled" for r in rows) == 1
        assert sum(r.active_key is not None for r in rows) == 1
        assert await db.count(filter_by(CourseAccess, user_id=FOO, course_id=c.id)) == 1
    assert (
        await ledger.fetchval(
            "SELECT count(*) FROM transactions WHERE id=ANY($1::uuid[])",
            [UUID(q1["offer"]["id"]), UUID(q2["offer"]["id"])],
        )
        == 1
    )
    # A later distinct quote prepared before the first completed cannot charge.
    try:
        await buy(c, accept(q2))
    except HTTPException as e:
        assert e.status_code == 409
    assert (
        await ledger.fetchval(
            "SELECT count(*) FROM transactions WHERE id=ANY($1::uuid[])",
            [UUID(q1["offer"]["id"]), UUID(q2["offer"]["id"])],
        )
        == 1
    )


async def test_lost_accept_response_then_deletion_does_not_recreate_access(ledger: Any, mocker: Any) -> None:
    c = course()
    q = await quote(c)
    a = accept(q)
    real = AsyncClient.post

    async def lost(self: Any, url: Any, *args: Any, **kwargs: Any) -> Any:
        response = await real(self, url, *args, **kwargs)
        if str(url).startswith("/purchases/skills/"):
            raise OSError("synthetic loss after remote committed success")
        return response

    mocker.patch.object(AsyncClient, "post", lost)
    with pytest.raises(OSError):
        await buy(c, a)
    mocker.patch.object(AsyncClient, "post", real)
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", a.order_id) == 1
    async with db_context():
        await delete_user_data(FOO)
    await purchases.recover()
    async with db_context():
        guard = await db.get(PurchaseUser, user_id=FOO)
        assert guard and guard.deleted
        assert await db.count(filter_by(CourseAccess, user_id=FOO, course_id=c.id)) == 0
        row = await db.get(CoursePurchase, id=str(a.order_id))
        assert row and row.state == "review" and row.active_key
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", a.order_id) == 1


async def test_source_completion_report_survives_backend_review(ledger: Any, mocker: Any) -> None:
    c = course()
    q = await quote(c)
    a = accept(q)
    original = purchases.report

    async def lose_report(row: Any) -> None:
        raise OSError("source commit succeeded, report lost")

    mocker.patch.object(purchases, "report", lose_report)
    with pytest.raises(OSError):
        await buy(c, a)
    await ledger.execute(
        "UPDATE purchase_progress SET state='review',"
        "review_reason='synthetic deletion review after source completion' WHERE order_id=$1",
        a.order_id,
    )
    mocker.patch.object(purchases, "report", original)
    await purchases.recover()
    async with db_context():
        row = await db.get(CoursePurchase, id=str(a.order_id))
        assert row and row.reported and row.state == "fulfilled"
    assert await ledger.fetchval("SELECT count(*) FROM purchase_fulfillments WHERE order_id=$1", a.order_id) == 1
    assert await ledger.fetchval("SELECT state FROM purchase_progress WHERE order_id=$1", a.order_id) == "review"


async def test_older_snapshot_cannot_charge_after_other_order_completed(ledger: Any) -> None:
    c = course()
    q1, q2 = await quote(c), await quote(c)
    snapshot_ready, completed = asyncio.Event(), asyncio.Event()

    async def older_snapshot() -> Any:
        async with db_context():
            assert not await db.exists(filter_by(CourseAccess, user_id=FOO, course_id=c.id))
            snapshot_ready.set()
            await completed.wait()
            try:
                return await purchases.buy(FOO, c, accept(q2))
            except HTTPException as error:
                assert error.status_code == 409
                return None

    waiter = asyncio.create_task(older_snapshot())
    await snapshot_ready.wait()
    result = await buy(c, accept(q1))
    assert result["state"] == "fulfilled"
    completed.set()
    await waiter
    assert (
        await ledger.fetchval(
            "SELECT count(*) FROM transactions WHERE id=ANY($1::uuid[])",
            [UUID(q1["offer"]["id"]), UUID(q2["offer"]["id"])],
        )
        == 1
    )


async def test_admission_reads_paid_rights_beyond_callers_old_snapshot(ledger: Any) -> None:
    from api.endpoints.course import get_owned_courses

    c = course()
    q = await quote(c)
    snapshot_ready, committed = asyncio.Event(), asyncio.Event()

    async def older_reader() -> None:
        async with db_context():
            if db.engine.dialect.name == "postgresql":
                await db.exec(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            else:
                assert db.engine.dialect.name == "mysql"
                assert (await db.exec(text("SELECT @@tx_isolation"))).scalar() == "REPEATABLE-READ"
            assert not await db.exists(filter_by(CourseAccess, user_id=FOO, course_id=c.id))
            snapshot_ready.set()
            await committed.wait()
            # Prove this caller still has the earlier database snapshot, then
            # exercise the exact shared reader used by course/list admission.
            assert not await db.exists(filter_by(CourseAccess, user_id=FOO, course_id=c.id))
            assert c.id in await get_owned_courses(FOO)

    task = asyncio.create_task(older_reader())
    await snapshot_ready.wait()
    try:
        result = await buy(c, accept(q))
        assert result["state"] == "fulfilled", result
    finally:
        committed.set()
    await task
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", UUID(q["offer"]["id"])) == 1


async def test_admission_observes_deletion_beyond_callers_old_positive_snapshot(ledger: Any) -> None:
    from api.endpoints.course import get_owned_courses

    c = course()
    q = await quote(c)
    assert (await buy(c, accept(q)))["state"] == "fulfilled"
    snapshot_ready, deleted = asyncio.Event(), asyncio.Event()

    async def older_reader() -> None:
        async with db_context():
            if db.engine.dialect.name == "postgresql":
                await db.exec(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            else:
                assert db.engine.dialect.name == "mysql"
                assert (await db.exec(text("SELECT @@tx_isolation"))).scalar() == "REPEATABLE-READ"
            assert await db.exists(filter_by(CourseAccess, user_id=FOO, course_id=c.id))
            snapshot_ready.set()
            await deleted.wait()
            assert await db.exists(filter_by(CourseAccess, user_id=FOO, course_id=c.id))
            assert c.id not in await get_owned_courses(FOO)

    task = asyncio.create_task(older_reader())
    await snapshot_ready.wait()
    try:
        async with db_context():
            await delete_user_data(FOO)
    finally:
        deleted.set()
    await task
    await purchases.recover()
    async with db_context():
        guard = await db.get(PurchaseUser, user_id=FOO)
        assert guard and guard.deleted
        assert c.id not in await get_owned_courses(FOO)
    assert await ledger.fetchval("SELECT count(*) FROM transactions WHERE id=$1", UUID(q["offer"]["id"])) == 1
