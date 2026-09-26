"""Durable lesson admission, separate from progress, purchases and completion."""

from datetime import date, datetime

from sqlalchemy import Boolean, Column, Date, Index, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class LessonStart(Base):
    __tablename__ = "skills_lesson_starts"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), primary_key=True)
    lesson_id: Mapped[str] = Column(String(256), primary_key=True)
    started_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    local_day: Mapped[date] = Column(Date, nullable=False)
    charged: Mapped[bool] = Column(Boolean, nullable=False)
    reason: Mapped[str] = Column(String(32), nullable=False)
    policy_mode: Mapped[str | None] = Column(String(16), nullable=True)
    __table_args__ = (
        Index("ix_lesson_starts_day", "user_id", "local_day", "charged"),
        {"mysql_collate": "utf8mb4_bin"},
    )


class LessonStartRequest(Base):
    __tablename__ = "skills_lesson_start_requests"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    request_id: Mapped[str] = Column(String(36), primary_key=True)
    course_id: Mapped[str] = Column(String(256), nullable=False)
    lesson_id: Mapped[str] = Column(String(256), nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)


class DailyLimitSettings(Base):
    __tablename__ = "skills_daily_limit_settings"
    id: Mapped[int] = Column(Integer, primary_key=True)
    mode: Mapped[str] = Column(String(16), nullable=False)
    limit: Mapped[int] = Column("lesson_limit", Integer, nullable=False)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    updated_by: Mapped[str] = Column(String(100), nullable=False)
    note: Mapped[str] = Column(String(512), nullable=False)
