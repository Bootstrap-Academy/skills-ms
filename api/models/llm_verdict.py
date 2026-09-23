"""Grading verdicts that completed a learning room; erased with the account.

One row per verdict that llm-ms signed and this service accepted. It makes every verdict single-use
and links the completion to llm-ms' grading receipt (same user and `request_id`). It never contains the
answer text, only its SHA-256.
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class LlmVerdict(Base):
    __tablename__ = "skills_llm_verdicts"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    request_id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), nullable=False)
    profile: Mapped[str] = Column(String(80), nullable=False)
    profile_sha256: Mapped[str] = Column(String(64), nullable=False)
    answer_sha256: Mapped[str] = Column(String(64), nullable=False)
    score: Mapped[int] = Column(Integer, nullable=False)
    max_score: Mapped[int] = Column(Integer, nullable=False)
    model: Mapped[str] = Column(String(128), nullable=False)
    graded_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    used_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
