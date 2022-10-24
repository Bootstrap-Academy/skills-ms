from datetime import datetime

from sqlalchemy import Column, String
from sqlalchemy.orm import Mapped

from api.database import Base, db
from api.database.database import UTCDateTime
from api.utils.utc import utcnow


class LastWatch(Base):
    __tablename__ = "skills_last_watch"

    user_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), primary_key=True)
    timestamp: Mapped[datetime] = Column(UTCDateTime)

    @classmethod
    async def update(cls, user_id: str, course_id: str) -> None:
        if row := await db.get(cls, user_id=user_id, course_id=course_id):
            row.timestamp = utcnow()
        else:
            await db.add(cls(user_id=user_id, course_id=course_id, timestamp=utcnow()))
