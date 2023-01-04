from typing import Any
from uuid import uuid4

from fastapi import APIRouter

from api import models
from api.auth import admin_auth, get_user, require_verified_email
from api.database import db, filter_by, select
from api.endpoints.skill import get_sub_skill
from api.exceptions.auth import PermissionDeniedError, admin_responses, verified_responses
from api.schemas.xp import RootSkillXP, SubSkillXP, UpdateXP, XPResponse
from api.services.xp import (
    calc_global_xp_needed,
    calc_progress,
    calc_root_skill_level,
    calc_root_skill_xp_needed,
    calc_sub_skill_level,
    calc_sub_skill_xp_needed,
)
from api.utils.cache import clear_cache, redis_cached


router = APIRouter()


@router.get(
    "/xp/{user_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(XPResponse, PermissionDeniedError),
)
@redis_cached("xp", "user_id")
async def get_xp(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Get a user's xp.


    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    xp_records: dict[str, int] = {
        row.skill_id: row.xp async for row in await db.stream(filter_by(models.XP, user_id=user_id))
    }

    response = XPResponse(total_xp=0, total_level=0, skills=[], progress=0)
    root_skill: models.RootSkill
    async for root_skill in await db.stream(select(models.RootSkill)):
        root_skill_xp = RootSkillXP(skill=root_skill.id, xp=0, level=0, skills=[], progress=0)
        for sub_skill in root_skill.sub_skills:
            xp = xp_records.get(sub_skill.id, 0)
            level = calc_sub_skill_level(xp)
            sub_skill_xp = SubSkillXP(
                skill=sub_skill.id, xp=xp, level=level, progress=calc_progress(xp, level, calc_sub_skill_xp_needed)
            )
            root_skill_xp.skills.append(sub_skill_xp)
            root_skill_xp.xp += xp

        root_skill_xp.level = calc_root_skill_level(root_skill_xp.xp)
        root_skill_xp.progress = calc_progress(root_skill_xp.xp, root_skill_xp.level, calc_root_skill_xp_needed)
        response.skills.append(root_skill_xp)
        response.total_xp += root_skill_xp.xp

    response.total_level = calc_root_skill_level(response.total_xp)
    response.progress = calc_progress(response.total_xp, response.total_level, calc_global_xp_needed)

    return response


# @router.get(
#     "/xp/{user_id}/{root_skill_id}/{sub_skill_id}",
#     dependencies=[require_verified_email],
#     responses=verified_responses(str, SkillNotCompletedError, UserNotFoundError),
# )
# @redis_cached("xp", "root_skill_id", "sub_skill_id", "user_id")
# async def get_certificate_code(
#     root_skill_id: str, sub_skill_id: str, user_id: str = get_user(check_existence=True, require_self_or_admin=True)
# ) -> Any:
#     """
#     Get a user's certificate code for a sub skill.
#
#     *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
#     """
#
#     skill: models.SubSkill = await get_sub_skill.dependency(root_skill_id, sub_skill_id)
#
#     xp_record = await db.get(models.XP, user_id=user_id, skill_id=skill.id)
#     if not xp_record or not xp_record.completed:
#         raise SkillNotCompletedError
#
#     return xp_record.id
#
#
# @router.get("/certificates/{code}", responses=responses(Certificate, CertificateNotFoundError))
# @redis_cached("xp", "code")
# async def get_certificate(code: str) -> Any:
#     """
#     Get a certificate by code.
#
#     You can get the code from the `GET /xp/{user_id}/{root_skill_id}/{sub_skill_id}` endpoint.
#     """
#
#     xp_record = await db.get(models.XP, id=code)
#     if not xp_record or not xp_record.completed:
#         raise CertificateNotFoundError
#
#     user = await get_user_for_certificate(xp_record.user_id)
#     sub_skill = await db.get(models.SubSkill, id=xp_record.skill_id)
#     if not user or not sub_skill:
#         raise CertificateNotFoundError
#
#     return Certificate(
#         user=user, sub_skill=sub_skill.serialize | {"completed": True}, root_skill=sub_skill.parent.serialize
#     )


@router.patch(
    "/xp/{user_id}/{root_skill_id}/{sub_skill_id}", dependencies=[admin_auth], responses=admin_responses(SubSkillXP)
)
async def update_xp(data: UpdateXP, skill: models.SubSkill = get_sub_skill, user_id: str = get_user()) -> Any:
    """
    Update a user's xp for a sub skill.

    *Requirements:* **ADMIN**
    """

    if xp := await db.get(models.XP, user_id=user_id, skill_id=skill.id):
        if data.xp is not None:
            xp.xp = data.xp
    else:
        xp = models.XP(id=str(uuid4()), user_id=user_id, skill_id=skill.id, xp=data.xp or 0)
        await db.add(xp)

    await clear_cache("xp")

    level = calc_sub_skill_level(xp.xp)
    return SubSkillXP(
        skill=skill.id, xp=xp.xp, level=level, progress=calc_progress(xp.xp, level, calc_sub_skill_xp_needed)
    )
