from datetime import datetime, timezone
from typing import Any

from api import models
from api.database import Base, db, db_context
from api.schemas.user_export import XP, CourseAccess, LastWatch, LectureProgress, SubSkillBookmark, UserDataExport
from api.services.user_export import export_user_data


TIMESTAMP = datetime(2026, 9, 3, 12, 34, 56, tzinfo=timezone.utc)

# maps every field of the export to the model it is read from
EXPORTED_MODELS: dict[str, Any] = {
    "room_states": models.RoomState,
    "room_requests": models.RoomRequest,
    "purchases": models.CoursePurchase,
    "purchase_user": models.PurchaseUser,
    "retained_course_rights": models.RetainedCourseRight,
    "course_right_grants": models.CourseRightGrant,
    "course_access": models.CourseAccess,
    "last_watch": models.LastWatch,
    "lecture_progress": models.LectureProgress,
    "sub_skill_bookmarks": models.SubSkillBookmark,
    "xp": models.XP,
    "xp_operations": models.XPOperation,
}


async def _add_user_data(user_id: str) -> None:
    await db.add(models.CourseAccess(user_id=user_id, course_id=f"course-{user_id}"))
    await db.add(models.LastWatch(user_id=user_id, course_id=f"course-{user_id}", timestamp=TIMESTAMP))
    await db.add(
        models.LectureProgress(
            user_id=user_id, course_id=f"course-{user_id}", lecture_id=f"lecture-{user_id}", completed=TIMESTAMP
        )
    )
    await db.add(
        models.SubSkillBookmark(user_id=user_id, root_skill_id=f"root-{user_id}", sub_skill_id=f"sub-{user_id}")
    )
    await db.add(
        models.XP(id=f"xp-{user_id}", user_id=user_id, skill_id=f"sub-{user_id}", xp=42, last_update=TIMESTAMP)
    )


def test__export_covers_every_table_with_user_data() -> None:
    assert set(EXPORTED_MODELS) == set(UserDataExport.__fields__)
    assert {model.__tablename__ for model in EXPORTED_MODELS.values()} == {
        table.name
        for table in Base.metadata.tables.values()
        if any(name in table.columns for name in ("user_id", "source_user_id", "subject"))
    }


async def test__export_user_data() -> None:
    async with db_context():
        await _add_user_data("user")
        await _add_user_data("other_user")

    async with db_context():
        export = await export_user_data("user")

    assert export == UserDataExport(
        course_access=[CourseAccess(course_id="course-user")],
        last_watch=[LastWatch(course_id="course-user", timestamp=TIMESTAMP)],
        lecture_progress=[LectureProgress(course_id="course-user", lecture_id="lecture-user", completed=TIMESTAMP)],
        sub_skill_bookmarks=[SubSkillBookmark(root_skill_id="root-user", sub_skill_id="sub-user")],
        xp=[XP(skill_id="sub-user", xp=42, last_update=TIMESTAMP)],
    )


async def test__export_user_data__unknown_user() -> None:
    async with db_context():
        await _add_user_data("other_user")

    async with db_context():
        export = await export_user_data("user")

    assert export == UserDataExport(course_access=[], last_watch=[], lecture_progress=[], sub_skill_bookmarks=[], xp=[])


async def test__export_retained_purchase_evidence_is_owner_bound() -> None:
    async with db_context():
        for owner in ["user", "other_user"]:
            await db.add(models.PurchaseUser(user_id=owner, deleted=True))
            await db.add(
                models.CoursePurchase(
                    id=f"purchase-{owner}",
                    user_id=owner,
                    course_id="course",
                    state="review",
                    active_key=f"{owner}:course",
                    offer={"recipient": f"{owner}@example.invalid"},
                    acceptance={"order_id": f"purchase-{owner}"},
                    result={"state": "paid"},
                    created_at=TIMESTAMP,
                    fulfillment=None,
                    reported=False,
                )
            )
    async with db_context():
        export = await export_user_data("user")
    assert export.purchase_user == [{"user_id": "user", "deleted": True}]
    assert len(export.purchases) == 1
    assert export.purchases[0]["id"] == "purchase-user"
    assert export.purchases[0]["result"] == {"state": "paid"}
    assert export.purchases[0]["fulfillment"] is None
    assert "other_user" not in export.json()


async def test__export_preserved_right_and_withdrawn_grant_remain_owner_bound() -> None:
    async with db_context():
        for owner in ["user", "other_user"]:
            await db.add(
                models.RetainedCourseRight(
                    id=f"right-{owner}",
                    source_user_id=owner,
                    course_id="course",
                    observed_at=TIMESTAMP,
                    original={"observed_started_course_access": True},
                    current_subject=None,
                    generation=1,
                )
            )
            await db.add(
                models.CourseRightGrant(
                    id=f"grant-{owner}",
                    right_id=f"right-{owner}",
                    subject=f"successor-{owner}",
                    request={"original_right": f"right-{owner}"},
                    state="withdrawn",
                    result={"state": "granted"},
                    created_at=TIMESTAMP,
                )
            )
    async with db_context():
        original_export = await export_user_data("user")
        successor_export = await export_user_data("successor-user")
        unrelated_export = await export_user_data("unrelated")
    for export in [original_export, successor_export]:
        assert [row["id"] for row in export.retained_course_rights] == ["right-user"]
        assert export.retained_course_rights[0]["original"] == {"observed_started_course_access": True}
        assert [row["id"] for row in export.course_right_grants] == ["grant-user"]
        assert export.course_right_grants[0]["state"] == "withdrawn"
        assert export.course_right_grants[0]["result"] == {"state": "granted"}
        assert export.last_watch == []
        assert "other_user" not in export.json()
    assert unrelated_export.retained_course_rights == []
    assert unrelated_export.course_right_grants == []
