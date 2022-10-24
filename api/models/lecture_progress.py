from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base, db, filter_by
from api.database.database import UTCDateTime
from api.utils.utc import utcnow


class LectureProgress(Base):
    __tablename__ = "skills_lecture_progress"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), primary_key=True)
    lecture_id: Mapped[str] = Column(String(256), primary_key=True)
    completed: Mapped[datetime] = Column(UTCDateTime)

    @classmethod
    async def get_completed(cls, user_id: str, course_id: str) -> set[str]:
        return {
            lecture.lecture_id
            async for lecture in await db.stream(filter_by(cls, user_id=user_id, course_id=course_id))
        }

    @classmethod
    async def set_completed(cls, user_id: str, course_id: str, lecture_id: str) -> None:
        await db.add(cls(user_id=user_id, course_id=course_id, lecture_id=lecture_id, completed=utcnow()))

    @classmethod
    async def is_completed(cls, user_id: str, course_id: str, lecture_id: str) -> bool:
        return await db.exists(filter_by(cls, user_id=user_id, course_id=course_id, lecture_id=lecture_id))
