from __future__ import annotations

from sqlalchemy import Column, Integer, String

from api.database import Base, db, filter_by
from api.exceptions.bookmarks import AlreadyBookmarkedException, BookmarkNotFoundException
from api.utils.cache import clear_cache


class SubSkillBookmark(Base):
    __tablename__ = "skills_sub_skill_bookmark"

    bookmark_id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(36))
    root_skill_id = Column(String(256))
    sub_skill_id = Column(String(256))

    @staticmethod
    async def create(user_id: str, root_skill_id: str, sub_skill_id: str) -> bool:
        if await db.exists(
            filter_by(SubSkillBookmark, user_id=user_id, sub_skill_id=sub_skill_id, root_skill_id=root_skill_id)
        ):
            raise AlreadyBookmarkedException

        bookmark = SubSkillBookmark(user_id=user_id, sub_skill_id=sub_skill_id, root_skill_id=root_skill_id)
        response = await db.add(bookmark)
        await clear_cache("skills")

        return bool(response)

    @staticmethod
    async def delete(user_id: str, root_skill_id: str, sub_skill_id: str) -> bool:
        bookmark = await db.get(
            SubSkillBookmark, user_id=user_id, sub_skill_id=sub_skill_id, root_skill_id=root_skill_id
        )
        if bookmark is None:
            raise BookmarkNotFoundException

        response = await db.delete(bookmark)
        await clear_cache("skills")

        return bool(response)
