from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import BigInteger, Column, ForeignKey, String, any_, asc, bindparam, desc, distinct, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.future import select as sa_select
from sqlalchemy.orm import Mapped, relationship
from sqlalchemy.sql import Select

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
        from fastapi import HTTPException

        from api.services.purchases import lock_user

        if (await lock_user(user_id)).deleted:
            raise HTTPException(404, "Learning data was erased")
        # Every XP writer shares deletion's subject guard. Refresh after waiting;
        # an earlier InnoDB snapshot must not overwrite another committed award.
        record = await db.first(
            filter_by(cls, user_id=user_id, skill_id=skill_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if record is None:
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
    def published_participants(cls, query: Select, participants: tuple[str, ...] | None) -> Select:
        """Constrain the comparison set before grouping, counting or pagination.

        PostgreSQL uses one array bind even for a large snapshot. None is the
        unchanged legacy set; an empty snapshot deliberately selects nobody.
        """
        if participants is None:
            return query
        if db.engine.dialect.name == "postgresql":
            return query.where(
                cls.user_id == any_(bindparam("published_user_ids", list(participants), type_=ARRAY(String(36))))
            )
        return query.where(cls.user_id.in_(participants))

    @classmethod
    async def rank_of(cls, xp: int, participants: tuple[str, ...] | None = None) -> int:
        return (
            await db.count(
                cls.published_participants(sa_select(XP.user_id).select_from(XP), participants)
                .group_by(XP.user_id)
                .having(func.sum(cls.xp) > xp)
            )
            or 0
        ) + 1

    @classmethod
    async def get_user_xp(cls, user: str) -> int:
        return await db.first(sa_select(func.sum(XP.xp)).select_from(XP).filter_by(user_id=user)) or 0

    @classmethod
    async def count_users(cls, participants: tuple[str, ...] | None = None) -> int:
        return (
            await db.first(
                cls.published_participants(sa_select(func.count(distinct(XP.user_id))).select_from(XP), participants)
            )
            or 0
        )

    @classmethod
    async def get_leaderboard(
        cls, limit: int, offset: int, participants: tuple[str, ...] | None = None
    ) -> list[tuple[str, int, int]]:  # id, xp, rank
        query = (
            cls.published_participants(sa_select(XP.user_id, func.sum(XP.xp).label("xp")), participants)
            .group_by(XP.user_id)
            .order_by(desc(func.sum(cls.xp)), asc(func.max(cls.last_update)))
        )
        if participants is not None:
            # Equal scores and timestamps must keep a stable order across pages.
            query = query.order_by(asc(cls.user_id))
        rows = [x async for x in await db.session.stream(query.limit(limit).offset(offset))]
        rank_xp = rows[0]["xp"] if rows else 0
        rank = await cls.rank_of(rank_xp, participants)
        out = []
        for i, (id, xp) in enumerate(rows):
            if xp < rank_xp:
                rank = offset + i + 1
                rank_xp = xp
            out.append((id, xp, rank))
        return out
