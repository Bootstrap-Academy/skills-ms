"""Endpoints related to the skilltree"""

from typing import Any

from fastapi import APIRouter

from api.auth import require_verified_email
from api.exceptions.auth import verified_responses
from api.exceptions.skill import SkillNotFoundException
from api.schemas.skill import RootSkill, SubSkill, SubSkillExtended
from api.services.skills import ID, SKILLS
from api.utils.docs import responses


router = APIRouter()


@router.get("/skilltree", responses=responses(list[RootSkill]))
async def list_skills() -> Any:
    """Return the root skill tree."""

    return [RootSkill(id=k, name=v.name, dependencies=v.dependencies, skills=[*v.skills]) for k, v in SKILLS.items()]


@router.get("/skilltree/{root_skill_id}", responses=responses(list[SubSkill], SkillNotFoundException))
async def get_skill(root_skill_id: ID) -> Any:
    """Return a specific sub skill tree."""

    if root_skill_id not in SKILLS:
        raise SkillNotFoundException

    root_skill = SKILLS[root_skill_id]

    return [
        SubSkill(id=k, name=v.name, dependencies=v.dependencies, courses=v.courses)
        for k, v in root_skill.skills.items()
    ]


@router.get(
    "/skilltree/{root_skill_id}/{sub_skill_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(SubSkillExtended, SkillNotFoundException),
)
async def get_subskill_details(root_skill_id: ID, sub_skill_id: ID) -> Any:
    """Return details about a specific sub skill."""

    if not (root_skill := SKILLS.get(root_skill_id)):
        raise SkillNotFoundException
    if not (sub_skill := root_skill.skills.get(sub_skill_id)):
        raise SkillNotFoundException

    return SubSkillExtended(
        id=sub_skill_id,
        name=sub_skill.name,
        dependencies=sub_skill.dependencies,
        courses=sub_skill.courses,
        coaches=[],  # todo
        exam_dates=[],  # todo
        webinars=[],  # todo
    )
