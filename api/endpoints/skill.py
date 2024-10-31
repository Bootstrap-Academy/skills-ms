"""Endpoints related to the skilltree"""

from typing import Any, cast

from fastapi import APIRouter, Depends

from api import models
from api.auth import admin_auth, public_auth
from api.database import db, filter_by, select
from api.exceptions.auth import admin_responses
from api.exceptions.course import CourseNotFoundException
from api.exceptions.skill import CycleInSkillTreeException, SkillAlreadyExistsException, SkillNotFoundException
from api.schemas.course import Course
from api.schemas.skill import (
    CreateRootSkill,
    CreateSubSkill,
    RootSkill,
    SkillTree,
    SubSkill,
    SubSkillTree,
    UpdateRootSkill,
    UpdateRootTree,
    UpdateSubSkill,
)
from api.schemas.user import User
from api.services.courses import COURSES
from api.utils.cache import clear_cache, redis_cached
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


def get_skill_dependents(skill_id: str, skills: dict[str, models.RootSkill] | dict[str, models.SubSkill]) -> set[str]:
    dependents: set[str] = set()
    queue = [skill_id]
    while queue:
        dependent_id = queue.pop()
        dependents.add(dependent_id)
        for dependent in skills[dependent_id].dependents:
            if dependent.id not in dependents:
                queue.append(dependent.id)
    return dependents


@router.get("/skilltree", responses=responses(SkillTree))
@redis_cached("skills")
async def list_root_skills() -> Any:
    """Return a list of all root skills."""

    settings = await models.TreeSettings.get()

    return SkillTree(
        skills=[skill.serialize async for skill in await db.stream(select(models.RootSkill))],
        rows=settings.rows,
        columns=settings.columns,
    )


@router.patch("/skilltree", dependencies=[admin_auth], responses=admin_responses(UpdateRootTree))
async def update_root_tree_settings(data: UpdateRootTree) -> Any:
    """
    Update the tree settings.

    *Requirements:* **ADMIN**
    """

    settings = await models.TreeSettings.get()

    if data.rows is not None and data.rows != settings.rows:
        settings.rows = data.rows
    if data.columns is not None and data.columns != settings.columns:
        settings.columns = data.columns

    await clear_cache("skills")

    return UpdateRootTree(rows=settings.rows, columns=settings.columns)


@router.post(
    "/skilltree",
    dependencies=[admin_auth],
    responses=admin_responses(RootSkill, SkillAlreadyExistsException, SkillNotFoundException),
)
async def create_root_skill(data: CreateRootSkill) -> Any:
    """
    Create a new root skill.

    *Requirements:* **ADMIN**
    """

    if await db.exists(filter_by(models.RootSkill, id=data.id)):
        raise SkillAlreadyExistsException

    dependencies = [await db.get(models.RootSkill, id=dependency) for dependency in data.dependencies]
    if None in dependencies:
        raise SkillNotFoundException

    skill = models.RootSkill(
        id=data.id,
        name=data.name,
        description=data.description,
        sub_skills=[],
        dependencies=cast(list[models.RootSkill], dependencies),
        row=data.row,
        column=data.column,
        sub_tree_rows=data.sub_tree_rows,
        sub_tree_columns=data.sub_tree_columns,
        icon=data.icon,
    )
    await db.add(skill)

    await clear_cache("skills")

    return skill.serialize


@router.patch(
    "/skilltree/{root_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(RootSkill, SkillNotFoundException, CycleInSkillTreeException),
)
async def update_root_skill(*, skill: models.RootSkill = get_root_skill, data: UpdateRootSkill) -> Any:
    """
    Update a root skill.

    *Requirements:* **ADMIN**
    """

    if data.name is not None and data.name != skill.name:
        skill.name = data.name

    if data.description is not None and data.description != skill.description:
        skill.description = data.description

    if data.dependencies is not None and data.dependencies != {dep.id for dep in skill.dependencies}:
        skills: dict[str, models.RootSkill] = {
            skill.id: skill async for skill in await db.stream(select(models.RootSkill))
        }
        dependencies = [*map(skills.get, data.dependencies)]
        if None in dependencies:
            raise SkillNotFoundException

        if get_skill_dependents(skill.id, skills) & data.dependencies:
            raise CycleInSkillTreeException

        skill.dependencies = cast(list[models.RootSkill], dependencies)

    if data.row is not None and data.row != skill.row:
        skill.row = data.row

    if data.column is not None and data.column != skill.column:
        skill.column = data.column

    if data.sub_tree_rows is not None and data.sub_tree_rows != skill.sub_tree_rows:
        skill.sub_tree_rows = data.sub_tree_rows

    if data.sub_tree_columns is not None and data.sub_tree_columns != skill.sub_tree_columns:
        skill.sub_tree_columns = data.sub_tree_columns

    if data.icon is not None and data.icon != skill.icon:
        skill.icon = data.icon

    await clear_cache("skills")

    return skill.serialize


@router.delete(
    "/skilltree/{root_skill_id}", dependencies=[admin_auth], responses=admin_responses(bool, SkillNotFoundException)
)
async def delete_root_skill(skill: models.RootSkill = get_root_skill) -> Any:
    """
    Delete a root skill.

    *Requirements:* **ADMIN**
    """

    await db.delete(skill)

    await clear_cache("skills")

    return True


@router.get("/skilltree/{root_skill_id}", responses=responses(SubSkillTree, SkillNotFoundException))
@redis_cached("skills", "root_skill_id", "user")
async def list_sub_skills(*, root_skill_id: str, user: User | None = public_auth) -> Any:
    """Return a list of all sub skills of a root skill."""

    root_skill: models.RootSkill = await get_root_skill.dependency(root_skill_id)

    return SubSkillTree(
        skills=[sub_skill.serialize for sub_skill in root_skill.sub_skills],
        rows=root_skill.sub_tree_rows,
        columns=root_skill.sub_tree_columns,
    )


@router.post(
    "/skilltree/{root_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(SubSkill, SkillAlreadyExistsException, SkillNotFoundException, CourseNotFoundException),
)
async def create_sub_skill(*, root_skill: models.RootSkill = get_root_skill, data: CreateSubSkill) -> Any:
    """
    Create a new sub skill in a root skill.

    *Requirements:* **ADMIN**
    """

    if await db.exists(filter_by(models.SubSkill, id=data.id)):
        raise SkillAlreadyExistsException

    dependencies = [
        await db.get(models.SubSkill, id=dependency, parent_id=root_skill.id) for dependency in data.dependencies
    ]
    if None in dependencies:
        raise SkillNotFoundException

    courses = [*map(COURSES.get, data.courses)]
    if None in courses:
        raise CourseNotFoundException

    skill = models.SubSkill(
        id=data.id,
        parent_id=root_skill.id,
        name=data.name,
        description=data.description,
        dependencies=cast(list[models.SubSkill], dependencies),
        courses=[models.SkillCourse(skill_id=data.id, course_id=course.id) for course in cast(list[Course], courses)],
        row=data.row,
        column=data.column,
        icon=data.icon,
    )
    await db.add(skill)

    await clear_cache("skills")

    return skill.serialize


@router.patch(
    "/skilltree/{root_skill_id}/{sub_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(SubSkill, SkillNotFoundException, CycleInSkillTreeException, CourseNotFoundException),
)
async def update_sub_skill(*, skill: models.SubSkill = get_sub_skill, data: UpdateSubSkill) -> Any:
    """
    Update a sub skill.

    *Requirements:* **ADMIN**
    """

    if data.name is not None and data.name != skill.name:
        skill.name = data.name

    if data.description is not None and data.description != skill.description:
        skill.description = data.description

    if data.dependencies is not None and data.dependencies != {dep.id for dep in skill.dependencies}:
        skills: dict[str, models.RootSkill] = {
            skill.id: skill async for skill in await db.stream(filter_by(models.SubSkill, parent_id=skill.parent_id))
        }
        dependencies = [*map(skills.get, data.dependencies)]
        if None in dependencies:
            raise SkillNotFoundException

        if get_skill_dependents(skill.id, skills) & data.dependencies:
            raise CycleInSkillTreeException

        skill.dependencies = cast(list[models.SubSkill], dependencies)

    course_ids = {course.course_id for course in skill.courses}
    if data.courses is not None and data.courses != course_ids:
        if any(course not in COURSES for course in data.courses):
            raise CourseNotFoundException

        skill.courses = [models.SkillCourse(skill_id=skill.id, course_id=course) for course in data.courses]

    if data.row is not None and data.row != skill.row:
        skill.row = data.row

    if data.column is not None and data.column != skill.column:
        skill.column = data.column

    if data.icon is not None and data.icon != skill.icon:
        skill.icon = data.icon

    await clear_cache("skills")

    return skill.serialize


@router.delete(
    "/skilltree/{root_skill_id}/{sub_skill_id}",
    dependencies=[admin_auth],
    responses=admin_responses(bool, SkillNotFoundException),
)
async def delete_sub_skill(skill: models.SubSkill = get_sub_skill) -> Any:
    """
    Delete a sub skill.

    *Requirements:* **ADMIN**
    """

    await db.delete(skill)

    await clear_cache("skills")

    return True
