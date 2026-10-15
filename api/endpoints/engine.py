"""Lesson engine: routes of stage 1. Behind `engine_enabled`, which stays off until the end-to-end check passes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response

from api.auth import admin_auth, require_verified_email, user_auth
from api.schemas.engine import ID_PATTERN, RevisionPublished, RevisionRequest, StartRequest, SubmitRequest
from api.schemas.user import User
from api.services import engine, engine_tasks
from api.settings import settings


async def enabled(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    if not settings.engine_enabled:
        raise HTTPException(404, "The lesson engine is unavailable")


router = APIRouter(prefix="/engine", dependencies=[Depends(enabled)])


@router.post("/admin/revisions", dependencies=[admin_auth], response_model=RevisionPublished)
async def publish_revision(data: RevisionRequest, response: Response) -> RevisionPublished:
    """Publish the server side of a package of the lesson engine and make it the unit's active revision.

    The same content again makes its earlier revision active (200); new content gets the next number (201).
    """
    published = await engine.publish(data)
    response.status_code = 201 if published.created else 200
    return published


UnitId = Path(pattern=ID_PATTERN)
GoalId = Path(pattern=ID_PATTERN)


@router.post("/units/{unit_id}/tasks/{goal_id}/start", dependencies=[require_verified_email])
async def start_task(
    data: StartRequest, unit_id: str = UnitId, goal_id: str = GoalId, user: User = user_auth
) -> dict[str, Any]:
    """Start a task, or get its open start again (§9). Answers `{start, assessments}` (Ä23)."""
    return await engine_tasks.start(unit_id, goal_id, data, user)


@router.post("/units/{unit_id}/tasks/{goal_id}/submissions", dependencies=[require_verified_email])
async def submit_plan(
    data: SubmitRequest, response: Response, unit_id: str = UnitId, goal_id: str = GoalId, user: User = user_auth
) -> dict[str, Any]:
    """Submit the saved plan for the open start; 202 while it is checked. The same plan again is the same submission."""
    view, response.status_code = await engine_tasks.submit(unit_id, goal_id, data, user)
    return view


@router.get("/submissions/{assessment_id}", dependencies=[require_verified_email])
async def read_submission(
    assessment_id: str = Path(pattern=r"^[0-9a-f-]{36}$"), user: User = user_auth
) -> dict[str, Any]:
    """The state of an own submission; another learner's is not found."""
    return await engine_tasks.status(assessment_id, user)
