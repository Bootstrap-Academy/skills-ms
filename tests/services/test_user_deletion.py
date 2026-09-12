from unittest.mock import AsyncMock, call

from pytest_mock import MockerFixture

from api import models
from api.database import Base, db, db_context, filter_by
from api.services.user_deletion import RETAINED_USER_MODELS, USER_CACHE_PREFIXES, USER_MODELS, delete_user_data
from api.utils.utc import utcnow


async def _add_user_data(user_id: str) -> None:
    await db.add(
        models.RoomState(
            user_id=user_id,
            unit_id="intro",
            revision=1,
            state={"step": 1},
            status="in_progress",
            result=None,
            updated_at=utcnow(),
        )
    )
    await db.add(
        models.RoomRequest(
            user_id=user_id,
            request_id="request",
            unit_id="intro",
            revision=1,
            fingerprint="f" * 64,
            progress={"revision": 1, "state": {"step": 1}, "status": "in_progress", "result": None},
            created_at=utcnow(),
        )
    )
    await db.add(models.CourseAccess(user_id=user_id, course_id="course"))
    await db.add(models.LastWatch(user_id=user_id, course_id="course", timestamp=utcnow()))
    await db.add(models.LectureProgress(user_id=user_id, course_id="course", lecture_id="lecture", completed=utcnow()))
    await db.add(models.SubSkillBookmark(user_id=user_id, root_skill_id="root", sub_skill_id="sub"))
    await db.add(models.XP(id=f"xp-{user_id}", user_id=user_id, skill_id="sub", xp=42, last_update=utcnow()))


def test__user_models() -> None:
    assert {model.__tablename__ for model in [*USER_MODELS, *RETAINED_USER_MODELS]} == {
        table.name
        for table in Base.metadata.tables.values()
        if any(key in table.columns for key in ("user_id", "source_user_id", "subject"))
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


async def test__erasure_preserves_both_existing_access_predicates_without_history(mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await db.add(models.CourseAccess(user_id="user", course_id="paid-access"))
        await db.add(models.LastWatch(user_id="user", course_id="started-access", timestamp=utcnow()))
        await db.add(models.CourseAccess(user_id="other", course_id="foreign-access"))
    async with db_context():
        await delete_user_data("user")
    async with db_context():
        rights = await db.all(filter_by(models.RetainedCourseRight, source_user_id="user"))
        assert {r.course_id for r in rights} == {"paid-access", "started-access"}
        original = {r.id: r.original for r in rights}
        paid = next(r for r in rights if r.course_id == "paid-access")
        started = next(r for r in rights if r.course_id == "started-access")
        assert paid.original["observed_course_access"] and not paid.original["observed_started_course_access"]
        assert started.original["observed_started_course_access"] and not started.original["observed_course_access"]
        assert all(r.original["viewing_history_retained"] is False and r.original["purchase_ids"] == [] for r in rights)
        assert not await db.exists(filter_by(models.LastWatch, user_id="user"))
        assert await db.exists(filter_by(models.CourseAccess, user_id="other"))
    async with db_context():
        await delete_user_data("user")
    async with db_context():
        rights = await db.all(filter_by(models.RetainedCourseRight, source_user_id="user"))
        assert {r.id: r.original for r in rights} == original


async def test__later_erasure_withdraws_current_grant_without_duplicate_right(mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await db.add(
            models.RetainedCourseRight(
                id="original-right",
                source_user_id="old",
                course_id="course",
                observed_at=utcnow(),
                original={"actual_prior_access": True},
                current_subject="fresh",
                generation=1,
            )
        )
        await db.add(
            models.CourseRightGrant(
                id="grant",
                right_id="original-right",
                subject="fresh",
                request={"existing_right": "original-right"},
                state="granted",
                result={"course_id": "course"},
                created_at=utcnow(),
            )
        )
        await db.add(models.CourseAccess(user_id="fresh", course_id="course"))
        await db.add(models.LastWatch(user_id="fresh", course_id="course", timestamp=utcnow()))
    async with db_context():
        await delete_user_data("fresh")
    async with db_context():
        right = await db.get(models.RetainedCourseRight, id="original-right")
        grant = await db.get(models.CourseRightGrant, id="grant")
        assert right is not None and grant is not None
        assert right.current_subject is None and right.generation == 2
        assert right.original == {"actual_prior_access": True}
        assert grant.state == "withdrawn" and grant.result == {"course_id": "course"}
        assert not await db.exists(filter_by(models.RetainedCourseRight, source_user_id="fresh"))
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))
