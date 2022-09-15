from __future__ import annotations

from sqlalchemy import Column, Integer
from sqlalchemy.orm import Mapped

from api.database import Base, db


class TreeSettings(Base):
    __tablename__ = "skills_tree_settings"

    rows: Mapped[int] = Column(Integer, primary_key=True)
    columns: Mapped[int] = Column(Integer, primary_key=True)

    @classmethod
    async def get(cls) -> TreeSettings:
        if not (obj := await db.get(cls)):
            obj = cls(rows=20, columns=20)
            await db.add(obj)
        return obj
