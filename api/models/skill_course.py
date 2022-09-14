from sqlalchemy import Column, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base
from api.models.sub_skill import SubSkill


class SkillCourse(Base):
    __tablename__ = "skills_skill_course"

    root_skill_id: Mapped[str] = Column(String(256), primary_key=True)
    sub_skill_id: Mapped[str] = Column(String(256), primary_key=True)
    sub_skill: SubSkill = relationship("SubSkill", back_populates="courses", lazy="selectin")
    course_id: Mapped[str] = Column(String(256), primary_key=True)

    __table_args__ = (
        ForeignKeyConstraint([root_skill_id, sub_skill_id], [SubSkill.parent_id, SubSkill.id]),
        Base.__table_args__,
    )  # type: ignore
