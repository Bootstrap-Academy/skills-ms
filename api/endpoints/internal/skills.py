from typing import Any

from fastapi import APIRouter, Body, Query

from api import models
from api.database import db, filter_by, select
from api.exceptions.skill import SkillNotFoundException
from api.schemas.skill import SubSkill
from api.utils.cache import clear_cache, redis_cached
from api.utils.docs import responses


router = APIRouter()


@router.get("/skills", responses=responses(list[SubSkill]))
@redis_cached("skills")
async def get_skills() -> Any:
    """Return a list of all skills."""

    return [skill.serialize async for skill in await db.stream(select(models.SubSkill))]


@router.get("/skills/{skill_id}/dependencies", responses=responses(set[str], SkillNotFoundException))
@redis_cached("skills", "skill_id")
async def get_skill_dependencies(skill_id: str) -> Any:
    """Return a list of all skills that are required to learn this skill."""

    skill = await db.get(models.SubSkill, id=skill_id)
    if not skill:
        raise SkillNotFoundException

    out = {s.id for s in skill.dependencies}
    for root in skill.parent.dependencies:
        out |= {s.id for s in root.sub_skills}

    return out


@router.get("/skills/{user_id}", responses=responses(dict[str, int]))
@redis_cached("xp", "user_id")
async def get_skill_levels(user_id: str) -> Any:
    return await models.XP.get_user_skill_levels(user_id)


@router.get("/graduates/{skill_id}", responses=responses(list[str]))
@redis_cached("xp", "skills")
async def get_graduates(skill_id: str, level: int = Query(ge=0)) -> Any:
    return await models.XP.get_skill_graduates(skill_id, level)


@router.post("/skills/{user_id}/{skill_id}", responses=responses(bool, SkillNotFoundException))
async def add_skill_progress(user_id: str, skill_id: str, xp: int = Body(embed=True)) -> Any:
    """Add progress to a skill for a user."""

    if not await db.exists(filter_by(models.SubSkill, id=skill_id)):
        raise SkillNotFoundException

    await models.XP.add_xp(user_id, skill_id, xp)

    await clear_cache("xp")

    return True
