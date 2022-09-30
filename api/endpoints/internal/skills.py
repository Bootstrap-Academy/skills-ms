from typing import Any

from fastapi import APIRouter

from api import models
from api.database import db, select
from api.utils.docs import responses


router = APIRouter()


@router.get("/skills", responses=responses(list[str]))
async def get_skills() -> Any:
    """Return a list of all skills."""

    return [skill.id async for skill in await db.stream(select(models.SubSkill))]


@router.get("/skills/{user_id}", responses=responses(list[str]))
async def get_completed_skills(user_id: str) -> Any:
    """Return a list of all completed skills for a user."""

    # todo
    return [skill.id async for skill in await db.stream(select(models.SubSkill))]
