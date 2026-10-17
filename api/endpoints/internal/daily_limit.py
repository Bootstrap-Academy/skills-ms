"""Trusted configuration and Challenges admission; never public user-selected scope."""

from typing import Any

from fastapi import APIRouter, HTTPException

from api.schemas.daily_limit import ChallengeAdmission, ChallengeReadBatch, LimitConfiguration
from api.services import daily_limit
from api.utils.docs import error_responses

# Every internal route answers 401 to a missing or wrong service token.
router = APIRouter(responses=error_responses(401))
# An admission can also be refused, conflict, hit the daily limit or find the learning access unreachable.
ADMISSION = error_responses(403, 404, 409, 429, 503)


@router.get("/daily-limit")
async def get_configuration() -> dict[str, Any]:
    mode, limit = await daily_limit.configuration()
    return {"mode": mode, "limit": limit, "activation_issues": daily_limit.activation_issues()}


@router.put("/daily-limit", responses=error_responses(409))
async def configure(data: LimitConfiguration) -> dict[str, Any]:
    return await daily_limit.configure(data)


@router.post("/learning-access/{user_id}/check", responses=ADMISSION)
async def check(user_id: str, data: ChallengeAdmission) -> dict[str, Any]:
    return await daily_limit.challenge_admission(user_id, data, False)


@router.post("/learning-access/{user_id}/check-batch", responses=error_responses(503))
async def check_batch(user_id: str, data: ChallengeReadBatch) -> dict[str, list[bool]]:
    """Keep each concrete decision and share the request's admission snapshot."""
    context = daily_limit.ChallengeReadContext()
    readable = []
    for item in data.requests:
        try:
            await daily_limit.challenge_admission(user_id, item, False, context)
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            readable.append(False)
        else:
            readable.append(True)
    return {"readable": readable}


@router.post("/learning-access/{user_id}/start", responses=ADMISSION)
async def start(user_id: str, data: ChallengeAdmission) -> dict[str, Any]:
    return await daily_limit.challenge_admission(user_id, data, True)


@router.post("/daily-limit/backfill/{user_id}")
async def backfill(user_id: str) -> dict[str, int]:
    return {"created": await daily_limit.backfill_user(user_id)}
