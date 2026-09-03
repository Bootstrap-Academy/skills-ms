from datetime import datetime, timezone
from typing import Any

from api import models
from api.database import Base, db, db_context
from api.schemas.user_export import XP, CourseAccess, LastWatch, LectureProgress, SubSkillBookmark, UserDataExport
from api.services.user_export import export_user_data


TIMESTAMP = datetime(2026, 9, 3, 12, 34, 56, tzinfo=timezone.utc)

# maps every field of the export to the model it is read from
EXPORTED_MODELS: dict[str, Any] = {
    "course_access": models.CourseAccess,
    "last_watch": models.LastWatch,
    "lecture_progress": models.LectureProgress,
    "sub_skill_bookmarks": models.SubSkillBookmark,
    "xp": models.XP,
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
        table.name for table in Base.metadata.tables.values() if "user_id" in table.columns
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
