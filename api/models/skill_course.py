from typing import Any

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.sub_skill import SubSkill


class SkillCourse(Base):
    __tablename__ = "skills_skill_course"

    skill_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)
    skill: SubSkill = relationship("SubSkill", back_populates="courses", lazy="selectin")
    course_id: Mapped[str] = Column(String(256), primary_key=True)

    @property
    def serialize(self) -> dict[str, Any]:
        return {"skill_id": self.skill_id, "skill": self.skill.serialize, "course_id": self.course_id}
