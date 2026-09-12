"""Private working state and exact request receipts; both erased with the account."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class RoomState(Base):
    __tablename__ = "skills_room_states"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), primary_key=True)
    revision: Mapped[int] = Column(Integer, nullable=False)
    state: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    status: Mapped[str] = Column(String(16), nullable=False)
    result: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    updated_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)


class RoomRequest(Base):
    __tablename__ = "skills_room_requests"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    request_id: Mapped[str] = Column(String(36), primary_key=True)
    unit_id: Mapped[str] = Column(String(80), nullable=False)
    revision: Mapped[int] = Column(Integer, nullable=False)
    fingerprint: Mapped[str] = Column(String(64), nullable=False)
    progress: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)
