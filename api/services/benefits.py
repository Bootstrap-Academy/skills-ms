"""Keyed XP receipt and effect share the caller's owning transaction.

These transport-authorized awards do not grant login or purchase authority.
An exact completed receipt precedes recipient lookup, including after erasure.
"""
import json
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, conint
from sqlalchemy.exc import IntegrityError

from api.database import db, filter_by
from api.models import SubSkill, XP, XPOperation
from api.services.auth import get_user_status
from api.services.purchases import lock_user
from api.utils.utc import utcnow


class XPAward(BaseModel):
    xp: conint(strict=True, ge=-9223372036854775808, le=9223372036854775807)
    earning_id: UUID

    class Config:
        extra = "forbid"


async def apply_xp(operation: str, user_id: str, skill_id: str, award: XPAward) -> dict[str, Any]:
    request = {"user_id": user_id, "skill_id": skill_id, **json.loads(award.json())}
    # Claim before the subject guard. A competing exact command waits for the
    # original transaction and then performs a current read even under InnoDB RR.
    try:
        async with db.session.begin_nested():
            await db.add(XPOperation(id=operation, user_id=user_id, request=request, received_at=utcnow()))
            await db.session.flush()
    except IntegrityError:
        pass
    row = await db.first(filter_by(XPOperation, id=operation).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise HTTPException(503, "Benefit receipt unavailable")
    if row.request != request:
        raise HTTPException(409, "Conflicting benefit operation")
    if row.result is not None:
        return row.result

    guard = await lock_user(user_id)
    if guard.deleted:
        # This is an actual local erasure marker. A remote 404 can instead mean
        # an unavailable joined recipient or route and must not create this fact.
        result = {"operation_id": operation, "request": request, "state": "recipient_erased", "applied": False}
    elif await get_user_status(user_id) == 200:
        if not await db.exists(filter_by(SubSkill, id=skill_id)):
            # No receipt/effect is committed for a temporarily unavailable skill.
            raise HTTPException(404, "Benefit skill unavailable")
        await XP.add_xp(user_id, skill_id, award.xp)
        result = {"operation_id": operation, "request": request, "state": "applied", "applied": True}
    else:
        raise HTTPException(503, "Benefit recipient status unavailable")
    row.result = result
    row.completed_at = utcnow()
    await db.session.flush()
    return result
