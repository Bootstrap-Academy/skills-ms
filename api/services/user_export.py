from api import models
from api.database import db, filter_by
from api.schemas.user_export import XP, CourseAccess, LastWatch, LectureProgress, SubSkillBookmark, UserDataExport


async def export_user_data(user_id: str) -> UserDataExport:
    """
    Collect everything this service stores about a user.

    Only rows that belong to the given user are read, so the export never contains data of anybody else. Has to be
    called inside a database context.
    """

    return UserDataExport(
        course_access=[
            CourseAccess(course_id=row.course_id)
            async for row in await db.stream(filter_by(models.CourseAccess, user_id=user_id))
        ],
        last_watch=[
            LastWatch(course_id=row.course_id, timestamp=row.timestamp)
            async for row in await db.stream(filter_by(models.LastWatch, user_id=user_id))
        ],
        lecture_progress=[
            LectureProgress(course_id=row.course_id, lecture_id=row.lecture_id, completed=row.completed)
            async for row in await db.stream(filter_by(models.LectureProgress, user_id=user_id))
        ],
        sub_skill_bookmarks=[
            SubSkillBookmark(root_skill_id=row.root_skill_id, sub_skill_id=row.sub_skill_id)
            async for row in await db.stream(filter_by(models.SubSkillBookmark, user_id=user_id))
        ],
        xp=[
            XP(skill_id=row.skill_id, xp=row.xp, last_update=row.last_update)
            async for row in await db.stream(filter_by(models.XP, user_id=user_id))
        ],
    )
