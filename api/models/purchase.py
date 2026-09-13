"""Source reservations survive lost responses and account erasure."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Column, String
from sqlalchemy.orm import Mapped

from api.database import Base
from api.database.database import UTCDateTime


class PurchaseUser(Base):
    __tablename__ = "skills_purchase_users"
    user_id: Mapped[str] = Column(String(36), primary_key=True)
    deleted: Mapped[bool] = Column(Boolean, nullable=False, default=False)


class CoursePurchase(Base):
    __tablename__ = "skills_course_purchases"
    id: Mapped[str] = Column(String(36), primary_key=True)
    user_id: Mapped[str] = Column(String(36), nullable=False, index=True)
    course_id: Mapped[str] = Column(String(256), nullable=False)
    state: Mapped[str] = Column(String(24), nullable=False, index=True)
    active_key: Mapped[str | None] = Column(String(512), nullable=True, unique=True)
    offer: Mapped[dict[str, Any]] = Column(JSON, nullable=False)
    acceptance: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    created_at: Mapped[datetime] = Column(UTCDateTime, nullable=False)

    fulfillment: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)
    reported: Mapped[bool] = Column(Boolean, nullable=False, default=False)
