from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.sub_skill import SubSkill


class SkillCourse(Base):
    __tablename__ = "skills_skill_course"

    skill_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"), primary_key=True)
    skill: SubSkill = relationship("SubSkill", back_populates="courses", lazy="selectin")
    course_id: Mapped[str] = Column(String(256), primary_key=True)
