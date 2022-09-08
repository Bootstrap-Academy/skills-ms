"""Endpoints related to the skilltree"""

from typing import Any

from fastapi import APIRouter

from api.exceptions.skill import SkillNotFoundException
from api.schemas.skill import RootSkill, SubSkill
from api.services.skills import SKILLS
from api.utils.docs import responses


router = APIRouter(tags=["skill"])


@router.get("/skilltree", responses=responses(list[RootSkill]))
async def list_skills() -> Any:
    """Return the root skill tree."""

    return [RootSkill(id=k, name=v.name, dependencies=v.dependencies, skills=[*v.skills]) for k, v in SKILLS.items()]


@router.get("/skilltree/{root_skill_id}", responses=responses(list[SubSkill], SkillNotFoundException))
async def get_skill(root_skill_id: str) -> Any:
    """Return a specific sub skill tree."""

    if root_skill_id not in SKILLS:
        raise SkillNotFoundException

    root_skill = SKILLS[root_skill_id]

    return [
        SubSkill(
            id=k,
            name=v.name,
            dependencies=v.dependencies,
            courses=v.courses,
            coaches=[],  # todo
            exam_dates=[],  # todo
            webinars=[],  # todo
        )
        for k, v in root_skill.skills.items()
    ]
