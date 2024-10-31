from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.root_skill import RootSkill
from api.schemas import skill as schemas


if TYPE_CHECKING:
    from .skill_course import SkillCourse
    from .xp import XP


class SubSkillDependency(Base):
    __tablename__ = "skills_sub_skill_dependency"

    dependent_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)
    dependency_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)


class SubSkill(Base):
    __tablename__ = "skills_sub_skill"

    id: Mapped[str] = Column(String(256), primary_key=True, unique=True)
    parent_id: Mapped[str] = Column(String(256), ForeignKey("skills_root_skill.id"))
    parent: RootSkill = relationship("RootSkill", back_populates="sub_skills", lazy="selectin")
    name: Mapped[str] = Column(String(256))
    description: Mapped[str | None] = Column(Text)
    row: Mapped[int] = Column(Integer)
    column: Mapped[int] = Column(Integer)
    icon: Mapped[str | None] = Column(String(256), nullable=True)
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
        "SkillCourse", back_populates="skill", lazy="selectin", cascade="all, delete-orphan"
    )
    xp: list[XP] = relationship("XP", back_populates="skill", cascade="all, delete-orphan")

    @property
    def serialize(self) -> schemas.SubSkill:
        return schemas.SubSkill(
            id=self.id,
            parent_id=self.parent_id,
            name=self.name,
            description=self.description,
            dependencies=[dependency.id for dependency in self.dependencies],
            dependents=[dependent.id for dependent in self.dependents],
            courses=[course.course_id for course in self.courses],
            row=self.row,
            column=self.column,
            icon=self.icon,
        )
