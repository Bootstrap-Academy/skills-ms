from typing import Any

from fastapi import APIRouter, Body, Query

from api import models
from api.database import db, filter_by, select
from api.exceptions.skill import SkillNotFoundException
from api.schemas.skill import SubSkill
from api.utils.docs import responses


router = APIRouter()


@router.get("/skills", responses=responses(list[SubSkill]))
async def get_skills() -> Any:
    """Return a list of all skills."""

    return [skill.serialize async for skill in await db.stream(select(models.SubSkill))]


@router.get("/skills/{user_id}", responses=responses(list[str]))
async def get_completed_skills(user_id: str) -> Any:
    """Return a list of all completed skills for a user."""

    return await models.XP.get_user_completed_skills(user_id)


@router.get("/graduates", responses=responses(list[str]))
async def get_graduates(skills: set[str] = Query()) -> Any:
    """Return a list of all users who have completed a skill."""

    out = set()
    for i, skill in enumerate(skills):
        if not i:
            out = await models.XP.get_skill_graduates(skill)
        else:
            out &= await models.XP.get_skill_graduates(skill)
    return out


@router.post("/skills/{user_id}/{skill_id}", responses=responses(bool, SkillNotFoundException))
async def add_skill_progress(user_id: str, skill_id: str, xp: int = Body(), complete: bool = Body()) -> Any:
    """Add progress to a skill for a user."""

    if not await db.exists(filter_by(models.SubSkill, id=skill_id)):
        raise SkillNotFoundException

    await models.XP.add_xp(user_id, skill_id, xp, complete)
    return True
