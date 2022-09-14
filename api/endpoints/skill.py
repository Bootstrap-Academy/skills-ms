"""Endpoints related to the skilltree"""

from typing import Any, cast

from fastapi import APIRouter, Depends

from api import models
from api.auth import admin_auth
from api.database import db, filter_by, select
from api.exceptions.auth import admin_responses
from api.exceptions.skill import CycleInSkillTreeException, SkillAlreadyExistsException, SkillNotFoundException
from api.schemas.skill import CreateRootSkill, RootSkill, UpdateRootSkill
from api.utils.docs import responses


router = APIRouter()


@Depends
async def get_root_skill(root_skill_id: str) -> models.RootSkill:
    """Get a root skill by ID."""

    root_skill: models.RootSkill | None = await db.get(models.RootSkill, id=root_skill_id)
    if root_skill is None:
        raise SkillNotFoundException

    return root_skill


@router.get("/skilltree", responses=responses(list[RootSkill]))
async def list_root_skills() -> Any:
    """Return a list of all root skills."""

    return [skill.serialize async for skill in await db.stream(select(models.RootSkill))]


@router.post(
    "/skilltree",
    dependencies=[admin_auth],
    responses=admin_responses(RootSkill, SkillAlreadyExistsException, SkillNotFoundException),
)
async def create_root_skill(data: CreateRootSkill) -> Any:
    """Create a new root skill."""

    if await db.exists(filter_by(models.RootSkill, id=data.id)):
        raise SkillAlreadyExistsException

    dependencies = [await db.get(models.RootSkill, id=dependency) for dependency in data.dependencies]
    if None in dependencies:
        raise SkillNotFoundException

    skill = models.RootSkill(
        id=data.id, name=data.name, sub_skills=[], dependencies=cast(list[models.RootSkill], dependencies)
    )
    await db.add(skill)
    return skill.serialize


@router.patch(
    "/skilltree/{root_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(RootSkill, SkillNotFoundException, CycleInSkillTreeException),
)
async def update_root_skill(*, skill: models.RootSkill = get_root_skill, data: UpdateRootSkill) -> Any:
    """Update a root skill."""

    if data.name is not None and data.name != skill.name:
        skill.name = data.name

    if data.dependencies is not None and data.dependencies != {dep.id for dep in skill.dependencies}:
        skills: dict[str, models.RootSkill] = {
            skill.id: skill async for skill in await db.stream(select(models.RootSkill))
        }
        dependencies = [*map(skills.get, data.dependencies)]
        if None in dependencies:
            raise SkillNotFoundException

        dependents = set()
        queue = [skill.id]
        while queue:
            dependent_id = queue.pop()
            dependents.add(dependent_id)
            for dependent in skills[dependent_id].dependents:
                if dependent.id not in dependents:
                    queue.append(dependent.id)

        if dependents & data.dependencies:
            raise CycleInSkillTreeException

        skill.dependencies = cast(list[models.RootSkill], dependencies)

    return skill.serialize


@router.delete(
    "/skilltree/{root_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(RootSkill, SkillNotFoundException),
)
async def delete_root_skill(skill: models.RootSkill = get_root_skill) -> Any:
    """Delete a root skill."""

    await db.delete(skill)
    return True
