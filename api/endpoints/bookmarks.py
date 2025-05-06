"""Endpoints related to bookmarking"""

from typing import Any

from fastapi import APIRouter, Depends

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db
from api.exceptions.skill import SkillNotFoundException
from api.schemas.user import User
from api.utils.docs import responses


router = APIRouter()


@Depends
async def get_root_skill(root_skill_id: str) -> models.RootSkill:
    """Get a root skill by ID."""

    root_skill: models.RootSkill | None = await db.get(models.RootSkill, id=root_skill_id)
    if root_skill is None:
        raise SkillNotFoundException

    return root_skill


@Depends
async def get_sub_skill(root_skill_id: str, sub_skill_id: str) -> models.SubSkill:
    """Get a sub skill by ID."""

    sub_skill: models.SubSkill | None = await db.get(models.SubSkill, id=sub_skill_id, parent_id=root_skill_id)
    if sub_skill is None:
        raise SkillNotFoundException

    return sub_skill


@router.post(
    "/bookmark/{root_skill_id}",
    dependencies=[require_verified_email],
    responses=responses(bool, SkillNotFoundException),
)
async def create_bookmark(user: User = user_auth, root_skill: models.RootSkill = get_root_skill) -> Any:
    """
    Create a new bookmark on a root skill.
    This bookmarks all subskills under the root skill.

    *Requirements:* **VERIFIED**
    """
    for subskill in root_skill.sub_skills:
        try:
            await create_sub_skill_bookmark(user, subskill)
        except Exception:  # noqa: S110
            pass

    return True


@router.delete(
    "/bookmark/{root_skill_id}",
    dependencies=[require_verified_email],
    responses=responses(bool, SkillNotFoundException),
)
async def delete_bookmark(user: User = user_auth, root_skill: models.RootSkill = get_root_skill) -> Any:
    """
    Delete a bookmark on a root skill.
    This deletes all subskill bookmarks under the root skill.

    *Requirements:* **VERIFIED**
    """
    for subskill in root_skill.sub_skills:
        try:
            await delete_sub_skill_bookmark(user, subskill)
        except Exception:  # noqa: S110
            pass

    return True


@router.post(
    "/bookmark/{root_skill_id}/{sub_skill_id}",
    dependencies=[require_verified_email],
    responses=responses(bool, SkillNotFoundException),
)
async def create_sub_skill_bookmark(user: User = user_auth, sub_skill: models.SubSkill = get_sub_skill) -> Any:
    """
    Create a new bookmark on a sub skill.
    This also bookmarks the root skill.

    *Requirements:* **VERIFIED**
    """
    return await models.SubSkillBookmark.create(
        user_id=user.id, root_skill_id=sub_skill.parent_id, sub_skill_id=sub_skill.id
    )


@router.delete(
    "/bookmark/{root_skill_id}/{sub_skill_id}",
    dependencies=[require_verified_email],
    responses=responses(bool, SkillNotFoundException),
)
async def delete_sub_skill_bookmark(user: User = user_auth, sub_skill: models.SubSkill = get_sub_skill) -> Any:
    """
    Delete a bookmark on a sub skill.
    This also deletes the root skill bookmark.

    *Requirements:* **VERIFIED**
    """
    return await models.SubSkillBookmark.delete(
        user_id=user.id, root_skill_id=sub_skill.parent_id, sub_skill_id=sub_skill.id
    )
