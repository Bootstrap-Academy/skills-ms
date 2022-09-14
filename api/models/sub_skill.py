from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.root_skill import RootSkill


if TYPE_CHECKING:
    from .skill_course import SkillCourse


class SubSkillDependency(Base):
    __tablename__ = "skills_sub_skill_dependency"

    dependent_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)
    dependency_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)


class SubSkill(Base):
    __tablename__ = "skills_sub_skill"

    id: Mapped[str] = Column(String(256), primary_key=True)
    parent_id: Mapped[str] = Column(String(256), ForeignKey("skills_root_skill.id"), primary_key=True)
    parent: RootSkill = relationship("RootSkill", back_populates="sub_skills", lazy="selectin")
    name: Mapped[str] = Column(String(256))
    dependencies: list[SubSkill] = relationship(
        "SubSkill",
        secondary="skills_sub_skill_dependency",
        primaryjoin=id == SubSkillDependency.dependent_id,
        secondaryjoin=SubSkillDependency.dependency_id == id,
        back_populates="dependents",
        lazy="selectin",
        join_depth=2,
    )
    dependents: list[SubSkill] = relationship(
        "SubSkill",
        secondary="skills_sub_skill_dependency",
        primaryjoin=id == SubSkillDependency.dependency_id,
        secondaryjoin=SubSkillDependency.dependent_id == id,
        back_populates="dependencies",
        lazy="selectin",
        join_depth=2,
    )
    courses: list[SkillCourse] = relationship(
        "SkillCourse", back_populates="sub_skill", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "dependencies": [dependency.id for dependency in self.dependencies],
            "dependents": [dependent.id for dependent in self.dependents],
            "courses": [course.course_id for course in self.courses],
        }
