"""Private course-wide project state (the bot card that grows across a course's lessons); erased with the account.

The request receipts never repeat the state: an exact retry carries the same state in its own body.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class CourseProject(Base):
    __tablename__ = "skills_course_projects"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), primary_key=True)
    revision: Mapped[int] = Column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)


class CourseProjectRequest(Base):
    __tablename__ = "skills_course_project_requests"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    request_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), nullable=False)
    revision: Mapped[int] = Column(Integer, nullable=False)
    fingerprint: Mapped[str] = Column(String(64), nullable=False)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
