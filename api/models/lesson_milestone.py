"""Outbox of lesson milestones (XP-02) for challenges-ms; erased with the account.

One row per learner and unit, written in the transaction that first completes the unit through its server
check. Delivery happens after that commit and is retried until challenges-ms answers for good, so a
completion never waits for or depends on challenges-ms. Resending is safe: challenges-ms books once per
learner and unit.
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class LessonMilestoneDelivery(Base):
    __tablename__ = "skills_lesson_milestones"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), primary_key=True)
    skill_id: Mapped[str] = Column(String(256), nullable=False)
    xp: Mapped[int] = Column(Integer, nullable=False)
    # `deterministic` (exact server-side answer check) or `llm_verdict` (signed passing verdict).
    completion: Mapped[str] = Column(String(16), nullable=False)
    # `pending` until challenges-ms answered: `delivered`, `erased` (410) or `rejected` (content error).
    state: Mapped[str] = Column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = Column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    last_status: Mapped[int | None] = Column(Integer, nullable=True)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
