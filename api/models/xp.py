from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import BigInteger, Column, ForeignKey, String, asc, desc, distinct, func
from sqlalchemy.future import select as sa_select
from sqlalchemy.orm import Mapped, relationship

from api.database import Base, db, filter_by
from api.database.database import UTCDateTime
from api.models import SubSkill
from api.services.xp import calc_sub_skill_level, calc_sub_skill_xp_needed
from api.utils.utc import utcnow


class XP(Base):
    __tablename__ = "skills_xp"

    id: Mapped[str] = Column(String(36), primary_key=True, unique=True)
    user_id: Mapped[str] = Column(String(36))
    skill_id: Mapped[str] = Column(String(256), ForeignKey("skills_sub_skill.id"))
    skill: SubSkill = relationship("SubSkill", back_populates="xp", lazy="selectin")
    xp: Mapped[int] = Column(BigInteger)
    last_update: Mapped[datetime] = Column(UTCDateTime)

    @classmethod
    async def add_xp(cls, user_id: str, skill_id: str, xp: int) -> None:
        if not (record := await db.get(cls, user_id=user_id, skill_id=skill_id)):
            record = XP(id=str(uuid4()), user_id=user_id, skill_id=skill_id, xp=0, last_update=utcnow())
            await db.add(record)
        record.xp += xp
        record.last_update = utcnow()

    @classmethod
    async def get_user_skill_xp(cls, user_id: str, skill_id: str) -> int:
        return sum(cast(XP, record).xp for record in await db.all(filter_by(cls, user_id=user_id, skill_id=skill_id)))

    @classmethod
    async def get_user_skill_levels(cls, user_id: str) -> dict[str, int]:
        return {
            record.skill_id: calc_sub_skill_level(record.xp)
            async for record in await db.stream(filter_by(cls, user_id=user_id))
        }

    @classmethod
    async def get_skill_graduates(cls, skill_id: str, level: int) -> set[str]:
        return {
            record.user_id
            async for record in await db.stream(
                filter_by(cls, skill_id=skill_id).where(XP.xp >= calc_sub_skill_xp_needed(level))
            )
        }

    @classmethod
    async def rank_of(cls, xp: int) -> int:
        return (
            await db.first(
                sa_select(func.count(distinct(XP.user_id)))
                .select_from(XP)
                .group_by(XP.user_id)
                .having(func.sum(cls.xp) > xp)
            )
            or 0
        ) + 1

    @classmethod
    async def get_user_xp(cls, user: str) -> int:
        return await db.first(sa_select(func.sum(XP.xp)).select_from(XP).filter_by(user_id=user)) or 0

    @classmethod
    async def count_users(cls) -> int:
        return await db.first(sa_select(func.count(distinct(XP.user_id))).select_from(XP)) or 0

    @classmethod
    async def get_leaderboard(cls, limit: int, offset: int) -> list[tuple[str, int, int]]:  # id, xp, rank
        rows = [
            x
            async for x in await db.session.stream(
                sa_select(XP.user_id, func.sum(XP.xp).label("xp"))
                .group_by(XP.user_id)
                .order_by(desc(func.sum(cls.xp)), asc(func.max(cls.last_update)))
                .limit(limit)
                .offset(offset)
            )
        ]
        rank_xp = rows[0]["xp"] if rows else 0
        rank = await cls.rank_of(rank_xp)
        out = []
        for i, (id, xp) in enumerate(rows):
            if xp < rank_xp:
                rank = offset + i + 1
                rank_xp = xp
            out.append((id, xp, rank))
        return out
