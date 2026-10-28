"""The learner's own learning plan ("Dein Lernweg") and suggestions for it; private to the learner."""

from fastapi import APIRouter, Header, Response

from api.auth import require_verified_email, user_auth
from api.schemas.learning_plan import Plan, SavePlan, Suggestion
from api.schemas.user import User
from api.services import learning_plan
from api.services.tracks import request_language
from api.utils.docs import error_responses

router = APIRouter(prefix="/plan", dependencies=[require_verified_email], responses=error_responses(401, 403, 503))


@router.get("", response_model=Plan)
async def get_plan(response: Response, user: User = user_auth, accept_language: str | None = Header(None)) -> Plan:
    """The learner's plan with the progress of every step; revision 0 and no steps before the first save.

    *Requirements:* **VERIFIED**
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await learning_plan.get_plan(user, request_language(accept_language))


@router.put("", response_model=Plan, responses=error_responses(409, 413, 422))
async def save_plan(
    data: SavePlan, response: Response, user: User = user_auth, accept_language: str | None = Header(None)
) -> Plan:
    """Replace the plan at `expected_revision`. A conflict (409) carries the current plan as `plan`; an exact retry of
    the latest `request_id` returns the same answer.

    *Requirements:* **VERIFIED**
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await learning_plan.save_plan(user, data, request_language(accept_language))


@router.get("/suggestions", response_model=list[Suggestion], response_model_exclude_none=True)
async def get_suggestions(
    response: Response, user: User = user_auth, accept_language: str | None = Header(None)
) -> list[Suggestion]:
    """Up to six courses for the plan, by character, preference, progress and plan; computed for each request and
    never stored.

    *Requirements:* **VERIFIED**
    """
    response.headers["Cache-Control"] = "private, no-store"
    return await learning_plan.suggestions(user, request_language(accept_language))
