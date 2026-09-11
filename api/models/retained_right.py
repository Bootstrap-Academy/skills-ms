"""Minimum observed course entitlement, independent of erased learning history."""
from datetime import datetime
from typing import Any
from sqlalchemy import JSON, Column, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped
from api.database import Base
from api.database.database import UTCDateTime


class RetainedCourseRight(Base):
    __tablename__ = "skills_retained_course_rights"
    __table_args__ = (UniqueConstraint("source_user_id", "course_id"), {"mysql_collate": "utf8mb4_bin"})
    id: Mapped[str] = Column(String(36), primary_key=True)
    source_user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    course_id: Mapped[str] = Column(String(256), nullable=False)
    observed_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    original: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    current_subject: Mapped[str | None] = Column(String(36), nullable=True, index=True)
    generation: Mapped[int] = Column(Integer, nullable=False, default=0)


class CourseRightGrant(Base):
    __tablename__ = "skills_course_right_grants"
    id: Mapped[str] = Column(String(36), primary_key=True)
    right_id: Mapped[str] = Column(String(36), ForeignKey("skills_retained_course_rights.id"), nullable=False)
    subject: Mapped[str] = Column(String(36), nullable=False, index=True)
    request: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    state: Mapped[str] = Column(String(24), nullable=False)
    result: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
