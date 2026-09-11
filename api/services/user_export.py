from api import models
from api.database import db, filter_by
from api.schemas.user_export import XP, CourseAccess, LastWatch, LectureProgress, SubSkillBookmark, UserDataExport
from sqlalchemy import or_
from api.database import select


async def export_user_data(user_id: str) -> UserDataExport:
    """
    Collect everything this service stores about a user.

    Only rows that belong to the given user are read, so the export never contains data of anybody else. Has to be
    called inside a database context.
    """

    return UserDataExport(
        xp_operations=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(filter_by(models.XPOperation, user_id=user_id))
        ],
        retained_course_rights=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(select(models.RetainedCourseRight).where(or_(
                models.RetainedCourseRight.source_user_id == user_id,
                models.RetainedCourseRight.current_subject == user_id,
                models.RetainedCourseRight.id.in_(select(models.CourseRightGrant.right_id).where(
                    models.CourseRightGrant.subject == user_id,
                )),
            )))
        ],
        course_right_grants=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(select(models.CourseRightGrant).where(or_(
                models.CourseRightGrant.subject == user_id,
                models.CourseRightGrant.right_id.in_(select(models.RetainedCourseRight.id).where(
                    models.RetainedCourseRight.source_user_id == user_id,
                )),
            )))
        ],
        purchases=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(filter_by(models.CoursePurchase, user_id=user_id))
        ],
        purchase_user=[
            {column.name: getattr(row, column.name) for column in row.__table__.columns}
            for row in await db.all(filter_by(models.PurchaseUser, user_id=user_id))
        ],
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
