from __future__ import annotations

from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base, db


class CourseAccess(Base):
    __tablename__ = "skills_course_access"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), primary_key=True)

    @staticmethod
    async def create(user_id: str, course_id: str) -> CourseAccess:
        obj = CourseAccess(user_id=user_id, course_id=course_id)
        await db.add(obj)
        return obj
