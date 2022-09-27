from typing import Any

from fastapi import APIRouter

from api import models
from api.auth import get_user, require_verified_email
from api.database import db, filter_by, select
from api.exceptions.auth import PermissionDeniedError, verified_responses
from api.schemas.xp import RootSkillXP, SubSkillXP, XPResponse
from api.services.xp import (
    calc_global_xp_needed,
    calc_progress,
    calc_root_skill_level,
    calc_root_skill_xp_needed,
    calc_sub_skill_level,
    calc_sub_skill_xp_needed,
)


router = APIRouter()


@router.get(
    "/xp/{user_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(XPResponse, PermissionDeniedError),
)
async def get_xp(user_id: str = get_user(require_self_or_admin=True)) -> Any:
    """
    Get a user's xp.


    *Requirements:* **VERIFIED** and (**SELF** or **ADMIN**)
    """

    xp_records = {row.skill_id: row.xp async for row in await db.stream(filter_by(models.XP, user_id=user_id))}

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
