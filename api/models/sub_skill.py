from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.root_skill import RootSkill


class SubSkill(Base):
    __tablename__ = "skills_sub_skill"

    id: Mapped[str] = Column(String(256), primary_key=True)
    parent_id: Mapped[str] = Column(String(256), ForeignKey("skills_root_skill.id"), primary_key=True)
    parent: RootSkill = relationship("RootSkill", back_populates="sub_skills")
    name: Mapped[str] = Column(String(256))
