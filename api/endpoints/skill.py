"""Endpoints related to the skilltree"""

from typing import Any

from fastapi import APIRouter

from api.schemas.skill import Skill
from api.services.skills import SKILLS
from api.utils.docs import responses


router = APIRouter(tags=["skill"])


@router.get("/skills", responses=responses(list[Skill]))
async def list_skills() -> Any:
    """Return a list of all skills."""

    return [
        Skill(id=k, name=v.name, courses=v.courses, instructors=[], exam_dates=[], dependencies=v.dependencies)
        for k, v in SKILLS.items()
    ]
