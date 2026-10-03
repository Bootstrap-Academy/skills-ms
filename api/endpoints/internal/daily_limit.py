"""Trusted configuration and Challenges admission; never public user-selected scope."""

from typing import Any

from fastapi import APIRouter

from api.schemas.daily_limit import ChallengeAdmission, LimitConfiguration
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


@router.post("/learning-access/{user_id}/start")
async def start(user_id: str, data: ChallengeAdmission) -> dict[str, Any]:
    return await daily_limit.challenge_admission(user_id, data, True)


@router.post("/daily-limit/backfill/{user_id}")
async def backfill(user_id: str) -> dict[str, int]:
    return {"created": await daily_limit.backfill_user(user_id)}
