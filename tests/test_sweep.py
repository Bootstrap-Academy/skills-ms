from unittest.mock import AsyncMock, MagicMock, call

from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api import models, sweep
from api.database import db, db_context
from api.services.internal import InternalServiceError
from api.settings import settings
from api.utils.utc import utcnow


async def test__user_ids_query() -> None:
    async with db_context():
        await db.add(models.CourseAccess(user_id="alice", course_id="course"))
        await db.add(models.LastWatch(user_id="bob", course_id="course", timestamp=utcnow()))
        await db.add(models.SubSkillBookmark(user_id="charlie", root_skill_id="root", sub_skill_id="sub"))
        await db.add(models.XP(id="xp", user_id="alice", skill_id="sub", xp=42, last_update=utcnow()))

    async with db_context():
        assert await db.all(sweep._user_ids_query(None, 2)) == ["alice", "bob"]
        assert await db.all(sweep._user_ids_query("bob", 2)) == ["charlie"]
        assert await db.all(sweep._user_ids_query("charlie", 2)) == []


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
    run_patch = mocker.patch("asyncio.run")
    sweep_patch = mocker.patch("api.sweep.sweep_deleted_users", MagicMock())

    sweep.main()

    run_patch.assert_called_once_with(sweep_patch.return_value)
