from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api import models, sweep
from api.database import db, db_context, filter_by, select
from api.services.internal import InternalServiceError
from api.settings import settings
from api.utils.utc import utcnow


async def test__user_ids_query() -> None:
    async with db_context():
        await db.add(models.CourseAccess(user_id="alice", course_id="course"))
        await db.add(models.LastWatch(user_id="bob", course_id="course", timestamp=utcnow()))
        await db.add(models.SubSkillBookmark(user_id="charlie", root_skill_id="root", sub_skill_id="sub"))
        await db.add(models.XP(id="xp", user_id="alice", skill_id="sub", xp=42, last_update=utcnow()))
        await db.add(models.XPOperation(id="operation", user_id="dora", request={}, received_at=utcnow()))

    async with db_context():
        assert await db.all(sweep._user_ids_query(None, 2)) == ["alice", "bob"]
        assert await db.all(sweep._user_ids_query("bob", 2)) == ["charlie", "dora"]
        assert await db.all(sweep._user_ids_query("dora", 2)) == []


async def test__sweep_deleted_users(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deleted_user_sweep_batch_size", 3)
    monkeypatch.setattr(settings, "deleted_user_sweep_rate_limit", 4)
    logger_patch = mocker.patch("api.sweep.logger")
    query_patch = mocker.patch("api.sweep._user_ids_query")
    mocker.patch("api.sweep.db_context")
    db_patch = mocker.patch("api.sweep.db")
    db_patch.all = AsyncMock(side_effect=[["a", "b", "c"], ["d", "e"], []])
    sleep_patch = mocker.patch("asyncio.sleep", AsyncMock())
    get_user_status_patch = mocker.patch(
        "api.sweep.get_user_status", AsyncMock(side_effect=[404, 200, InternalServiceError(), 400, 404])
    )
    delete_user_data_patch = mocker.patch("api.sweep.delete_user_data", AsyncMock())

    await sweep.sweep_deleted_users()

    assert query_patch.call_args_list == [call(None, 3), call("c", 3), call("e", 3)]
    assert db_patch.all.await_args_list == [call(query_patch())] * 3
    assert sleep_patch.await_args_list == [call(0.25)] * 5
    assert get_user_status_patch.await_args_list == [call(x) for x in ["a", "b", "c", "d", "e"]]
    assert delete_user_data_patch.await_args_list == [call("a"), call("e")]
    logger_patch.info.assert_called_once_with("sweep finished: checked=5 missing=2 deleted=2 errors=2")


async def test__sweep_deleted_users__no_rate_limit(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deleted_user_sweep_rate_limit", 0)
    mocker.patch("api.sweep._user_ids_query")
    mocker.patch("api.sweep.db_context")
    db_patch = mocker.patch("api.sweep.db")
    db_patch.all = AsyncMock(side_effect=[["a"], []])
    sleep_patch = mocker.patch("asyncio.sleep", AsyncMock())
    mocker.patch("api.sweep.get_user_status", AsyncMock(return_value=200))

    await sweep.sweep_deleted_users()

    assert sleep_patch.await_args_list == [call(0)]


async def test__main(mocker: MockerFixture) -> None:
    # The connections must be released inside the loop that `asyncio.run` drives. Left to the garbage
    # collector, a Redis connection that the erasure opened prints "RuntimeError: Event loop is closed".
    steps: list[str] = []
    mocker.patch("api.sweep.sweep_deleted_users", AsyncMock(side_effect=lambda: steps.append("sweep")))
    mocker.patch("api.sweep.close_redis", AsyncMock(side_effect=lambda: steps.append("redis")))
    mocker.patch.object(db, "dispose", AsyncMock(side_effect=lambda: steps.append("database")))
    run_patch = mocker.patch("asyncio.run")

    sweep.main()

    run_patch.assert_called_once()
    assert steps == []
    await run_patch.call_args.args[0]
    assert steps == ["sweep", "redis", "database"]


async def test__run_sweep__releases_the_connections_when_the_sweep_fails(mocker: MockerFixture) -> None:
    mocker.patch("api.sweep.sweep_deleted_users", AsyncMock(side_effect=RuntimeError("sweep failed")))
    close_redis_patch = mocker.patch("api.sweep.close_redis", AsyncMock(side_effect=ConnectionError("redis gone")))
    dispose_patch = mocker.patch.object(db, "dispose", AsyncMock())

    with pytest.raises(ConnectionError):
        await sweep.run_sweep()

    close_redis_patch.assert_awaited_once_with()
    dispose_patch.assert_awaited_once_with()


async def test__sweep_removes_receipts_left_by_an_earlier_erasure(
    mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    # Before receipts went with the account, an erasure left the tombstone and the
    # receipts and nothing else. The sweep reaches such an account through its receipts.
    monkeypatch.setattr(settings, "deleted_user_sweep_rate_limit", 0)
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await db.add(models.PurchaseUser(user_id="erased", deleted=True))
        for user_id in ("erased", "erased", "alive"):
            await db.add(
                models.XPOperation(
                    id=str(uuid4()),
                    user_id=user_id,
                    request={"user_id": user_id},
                    result={"state": "applied", "applied": True},
                    received_at=utcnow(),
                    completed_at=utcnow(),
                )
            )
        await db.add(models.XP(id="xp-alive", user_id="alive", skill_id="sub", xp=42, last_update=utcnow()))
    status = mocker.patch(
        "api.sweep.get_user_status", AsyncMock(side_effect=lambda user_id: 404 if user_id == "erased" else 200)
    )
    logger = mocker.patch("api.sweep.logger")

    await sweep.sweep_deleted_users()

    assert sorted(item.args[0] for item in status.await_args_list) == ["alive", "erased"]
    logger.info.assert_called_once_with("sweep finished: checked=2 missing=1 deleted=1 errors=0")
    async with db_context():
        assert [row.user_id for row in await db.all(select(models.XPOperation))] == ["alive"]
        assert await db.exists(filter_by(models.XP, user_id="alive"))
        guard = await db.get(models.PurchaseUser, user_id="erased")
        assert guard is not None and guard.deleted
    status.reset_mock()
    await sweep.sweep_deleted_users()
    assert [item.args[0] for item in status.await_args_list] == ["alive"]


async def test__one_cache_failure_rolls_back_and_does_not_starve_next_user(
    mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "deleted_user_sweep_rate_limit", 0)
    monkeypatch.setattr(settings, "deleted_user_sweep_batch_size", 1)
    async with db_context():
        await db.add(models.CourseAccess(user_id="a", course_id="course"))
        await db.add(models.CourseAccess(user_id="z", course_id="course"))
    mocker.patch("api.sweep.get_user_status", AsyncMock(return_value=404))
    cache = mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    cache.side_effect = [RuntimeError("synthetic cache outage"), *[None] * 5]
    await sweep.sweep_deleted_users()
    async with db_context():
        assert [row.user_id for row in await db.all(select(models.CourseAccess))] == ["a"]
    cache.side_effect = None
    await sweep.sweep_deleted_users()
    async with db_context():
        assert await db.all(select(models.CourseAccess)) == []
