"""Operator-published code references; no learner data or executable server code."""

from sqlalchemy import CheckConstraint, Column, Integer, String
from sqlalchemy.orm import Mapped

from api.database import Base


class LessonModule(Base):
    __tablename__ = "skills_lesson_modules"
    __table_args__ = (
        CheckConstraint("api_version = 1", name="ck_lesson_module_api_version"),
        {"mysql_collate": "utf8mb4_bin"},
    )

    id: Mapped[str] = Column(String(128), primary_key=True)
    api_version: Mapped[int] = Column(Integer, nullable=False)
    entry_url: Mapped[str] = Column(String(2048), nullable=False)
