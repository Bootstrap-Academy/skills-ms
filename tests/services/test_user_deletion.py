from unittest.mock import AsyncMock, call

from pytest_mock import MockerFixture

from api import models
from api.database import Base, db, db_context, filter_by
from api.services.user_deletion import RETAINED_USER_MODELS, USER_CACHE_PREFIXES, USER_MODELS, delete_user_data
from api.utils.utc import utcnow


async def _add_user_data(user_id: str) -> None:
    await db.add(models.CourseAccess(user_id=user_id, course_id="course"))
    await db.add(models.LastWatch(user_id=user_id, course_id="course", timestamp=utcnow()))
    await db.add(models.LectureProgress(user_id=user_id, course_id="course", lecture_id="lecture", completed=utcnow()))
    await db.add(models.SubSkillBookmark(user_id=user_id, root_skill_id="root", sub_skill_id="sub"))
    await db.add(models.XP(id=f"xp-{user_id}", user_id=user_id, skill_id="sub", xp=42, last_update=utcnow()))


def test__user_models() -> None:
    assert {model.__tablename__ for model in [*USER_MODELS, *RETAINED_USER_MODELS]} == {
        table.name for table in Base.metadata.tables.values() if "user_id" in table.columns
    }


async def test__delete_user_data(mocker: MockerFixture) -> None:
    clear_cache = mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await _add_user_data("user")
        await _add_user_data("other_user")

    async with db_context():
        await delete_user_data("user")

    async with db_context():
        for model in USER_MODELS:
            assert not await db.exists(filter_by(model, user_id="user"))
            assert await db.exists(filter_by(model, user_id="other_user"))

    assert clear_cache.await_args_list == [call(prefix) for prefix in USER_CACHE_PREFIXES]


async def test__delete_user_data__unknown_user(mocker: MockerFixture) -> None:
    clear_cache = mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await _add_user_data("other_user")

    async with db_context():
        await delete_user_data("user")

    async with db_context():
        for model in USER_MODELS:
            assert await db.exists(filter_by(model, user_id="other_user"))

    assert clear_cache.await_args_list == [call(prefix) for prefix in USER_CACHE_PREFIXES]


async def test__deletion_retains_unperformed_claim_and_tombstone(mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await _add_user_data("user")
        await db.add(
            models.CoursePurchase(
                id="paid-order",
                user_id="user",
                course_id="course",
                state="paid",
                active_key="user:course",
                offer={"coins": 100},
                acceptance={"order_id": "paid-order"},
                result={"state": "paid", "financial_evidence": {"paid_coins": 100}},
                created_at=utcnow(),
                fulfillment=None,
                reported=False,
            )
        )
    async with db_context():
        await delete_user_data("user")
    async with db_context():
        guard = await db.get(models.PurchaseUser, user_id="user")
        purchase = await db.get(models.CoursePurchase, id="paid-order")
        assert guard and guard.deleted
        assert purchase and purchase.state == "review"
        assert purchase.active_key == "user:course"
        assert purchase.result == {"state": "paid", "financial_evidence": {"paid_coins": 100}}
        assert purchase.fulfillment is None
        assert not await db.exists(filter_by(models.CourseAccess, user_id="user"))
