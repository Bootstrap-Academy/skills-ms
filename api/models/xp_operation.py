"""Exact benefit receipts survive removal of the mutable XP counter."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class XPOperation(Base):
    __tablename__ = "skills_xp_operations"
    id: Mapped[str] = Column(String(36), primary_key=True)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    request: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    result: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    received_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = Column(UTCDateTime, nullable=True)
