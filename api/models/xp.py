from typing import cast
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base, db, filter_by
from api.models import SubSkill
from api.services.xp import calc_sub_skill_level, calc_sub_skill_xp_needed


class XP(Base):
    __tablename__ = "skills_xp"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    skill_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"))
    skill: SubSkill = relationship("SubSkill", back_populates="xp", lazy="selectin")
    xp: Mapped[int] = Column(BigInteger)
    completed: Mapped[bool] = Column(Boolean)

    @classmethod
    async def add_xp(cls, user_id: str, skill_id: str, xp: int, complete: bool = False) -> None:
        if not (record := await db.get(cls, user_id=user_id, skill_id=skill_id)):
            record = XP(id=str(uuid4()), user_id=user_id, skill_id=skill_id, xp=0, completed=False)
            await db.add(record)
        record.xp += xp
        record.completed = record.completed or complete

    @classmethod
    async def get_user_xp(cls, user_id: str) -> int:
        return sum(cast(XP, record).xp for record in await db.all(filter_by(cls, user_id=user_id)))

    @classmethod
    async def get_user_skill_xp(cls, user_id: str, skill_id: str) -> int:
        return sum(cast(XP, record).xp for record in await db.all(filter_by(cls, user_id=user_id, skill_id=skill_id)))

    @classmethod
    async def get_user_skill_levels(cls, user_id: str) -> dict[str, int]:
        return {
            record.skill_id: calc_sub_skill_level(record.xp)
            async for record in await db.stream(filter_by(cls, user_id=user_id, completed=True))
        }

    @classmethod
    async def get_skill_graduates(cls, skill_id: str, level: int) -> set[str]:
        return {
            record.user_id
            async for record in await db.stream(
                filter_by(cls, skill_id=skill_id).where(XP.xp >= calc_sub_skill_xp_needed(level))
            )
        }
