"""A learner's own learning plan ("Dein Lernweg"): one row per learner, without history; erased with the account.

The state holds the self-chosen character and preference and the ordered steps, with the self-confirmed "done" of
external steps. `request_id` and `request_fingerprint` belong to the latest save only, so its exact retry gets the
same answer; there is no table of receipts.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class LearningPlan(Base):
    __tablename__ = "skills_learning_plans"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    revision: Mapped[int] = Column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    request_id: Mapped[str] = Column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = Column(String(64), nullable=False)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
