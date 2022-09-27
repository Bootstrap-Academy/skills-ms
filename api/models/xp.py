from typing import cast
from uuid import uuid4

from sqlalchemy import BigInteger, Column, ForeignKey, String
from sqlalchemy.orm import Mapped, relationship

from api.database import Base, db, filter_by
from api.models import SubSkill


class XP(Base):
    __tablename__ = "skills_xp"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    skill_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"))
    skill: SubSkill = relationship("SubSkill", back_populates="xp", lazy="selectin")
    xp: Mapped[int] = Column(BigInteger)

    @classmethod
    async def add_xp(cls, user_id: str, skill_id: str, xp: int) -> None:
        if record := await db.get(cls, user_id=user_id, skill_id=skill_id):
            record.xp += xp
        else:
            await db.add(XP(id=str(uuid4()), user_id=user_id, skill_id=skill_id, xp=xp))

    @classmethod
    async def get_user_xp(cls, user_id: str) -> int:
        return sum(cast(XP, record).xp for record in await db.all(filter_by(cls, user_id=user_id)))

    @classmethod
    async def get_user_skill_xp(cls, user_id: str, skill_id: str) -> int:
        return sum(cast(XP, record).xp for record in await db.all(filter_by(cls, user_id=user_id, skill_id=skill_id)))
