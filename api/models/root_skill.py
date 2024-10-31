from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, relationship

from api.database import Base


if TYPE_CHECKING:
    from .sub_skill import SubSkill


class RootSkillDependency(Base):
    __tablename__ = "skills_root_skill_dependency"

    dependent_id: Mapped[str] = Column(String(256), ForeignKey("skills_root_skill.id"), primary_key=True)
    dependency_id: Mapped[str] = Column(String(256), ForeignKey("skills_root_skill.id"), primary_key=True)


class RootSkill(Base):
    __tablename__ = "skills_root_skill"

    id: Mapped[str] = Column(String(256), primary_key=True, unique=True)
    name: Mapped[str] = Column(String(256))
    description: Mapped[str | None] = Column(Text)
    row: Mapped[int] = Column(Integer)
    column: Mapped[int] = Column(Integer)
    sub_tree_rows: Mapped[int] = Column(Integer)
    sub_tree_columns: Mapped[int] = Column(Integer)
    icon: Mapped[str | None] = Column(String(256), nullable=True)
    sub_skills: list[SubSkill] = relationship(
        "SubSkill", back_populates="parent", cascade="all, delete", lazy="selectin", join_depth=3
    )
    dependencies: list[RootSkill] = relationship(
        "RootSkill",
        secondary="skills_root_skill_dependency",
        primaryjoin=id == RootSkillDependency.dependent_id,
        secondaryjoin=RootSkillDependency.dependency_id == id,
        back_populates="dependents",
        lazy="selectin",
        join_depth=2,
    )
    dependents: list[RootSkill] = relationship(
        "RootSkill",
        secondary="skills_root_skill_dependency",
        primaryjoin=id == RootSkillDependency.dependency_id,
        secondaryjoin=RootSkillDependency.dependent_id == id,
        back_populates="dependencies",
        lazy="selectin",
        join_depth=2,
    )

    @property
    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "dependencies": [dependency.id for dependency in self.dependencies],
            "dependents": [dependent.id for dependent in self.dependents],
            "skills": [sub_skill.id for sub_skill in self.sub_skills],
            "row": self.row,
            "column": self.column,
            "sub_tree_rows": self.sub_tree_rows,
            "sub_tree_columns": self.sub_tree_columns,
            "icon": self.icon,
        }
