"""Trusted configuration and Challenges admission; never public user-selected scope."""

from typing import Any

from fastapi import APIRouter, HTTPException

from api.schemas.daily_limit import ChallengeAdmission, ChallengeReadBatch, LimitConfiguration
from api.services import daily_limit


router = APIRouter()


@router.get("/daily-limit")
async def get_configuration() -> dict[str, Any]:
    mode, limit = await daily_limit.configuration()
    return {"mode": mode, "limit": limit, "activation_issues": daily_limit.activation_issues()}


@router.put("/daily-limit")
async def configure(data: LimitConfiguration) -> dict[str, Any]:
    return await daily_limit.configure(data)


@router.post("/learning-access/{user_id}/check")
async def check(user_id: str, data: ChallengeAdmission) -> dict[str, Any]:
    return await daily_limit.challenge_admission(user_id, data, False)


@router.post("/learning-access/{user_id}/check-batch")
async def check_batch(user_id: str, data: ChallengeReadBatch) -> dict[str, list[bool]]:
    """Keep each concrete decision and share the request's admission snapshot."""
    readable = []
    for item in data.requests:
        try:
            await daily_limit.challenge_admission(user_id, item, False)
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            readable.append(False)
        else:
            readable.append(True)
    return {"readable": readable}


@router.post("/learning-access/{user_id}/start")
async def start(user_id: str, data: ChallengeAdmission) -> dict[str, Any]:
    return await daily_limit.challenge_admission(user_id, data, True)


@router.post("/daily-limit/backfill/{user_id}")
async def backfill(user_id: str) -> dict[str, int]:
    return {"created": await daily_limit.backfill_user(user_id)}
