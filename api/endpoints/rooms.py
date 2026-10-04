"""Private, curated learning rooms shared by current and future clients."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.auth import get_token, require_verified_email, user_auth
from api.database import db
from api.schemas.rooms import Complete, LlmGrant, RoomEnvelope, Rooms, SaveState, StartReview
from api.schemas.user import User
from api.services import lesson_milestones, rooms
from api.settings import settings

router = APIRouter()


async def enabled(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"
    if not settings.rooms_enabled:
        raise HTTPException(404, "Learning rooms are unavailable")


@router.get("/rooms/capabilities")
async def capabilities(response: Response) -> dict[str, bool]:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled": settings.rooms_enabled}


private = APIRouter(prefix="/rooms", dependencies=[Depends(enabled), require_verified_email])


@private.get("", response_model=Rooms)
async def next_room(
    request: Request,
    path: str = "python-loops",
    after: str | None = None,
    continuous: bool = False,
    direction: str | None = None,
    course: str | None = None,
    unit: str | None = None,
    user: User = user_auth,
) -> Rooms:
    return await rooms.next_room(user, get_token(request), path, after, continuous, direction, course, unit)


@private.get("/{unit_id}", response_model=RoomEnvelope)
async def get_room(unit_id: str, request: Request, course: str | None = None, user: User = user_auth) -> RoomEnvelope:
    return await rooms.get_room(unit_id, user, get_token(request), course)


@private.put("/{unit_id}/state", response_model=RoomEnvelope)
async def save_state(
    unit_id: str, data: SaveState, request: Request, course: str | None = None, user: User = user_auth
) -> RoomEnvelope:
    return await rooms.mutate_room(unit_id, user, get_token(request), data, course)


@private.post("/{unit_id}/complete", response_model=RoomEnvelope)
async def complete(
    unit_id: str, data: Complete, request: Request, course: str | None = None, user: User = user_auth
) -> RoomEnvelope:
    lesson_milestones.take_queued()
    envelope = await rooms.mutate_room(unit_id, user, get_token(request), data, course)
    if queued := lesson_milestones.take_queued():
        # The completion and its lesson milestone are durable before challenges-ms is called.
        await db.commit()
        lesson_milestones.deliver_soon(*queued)
    return envelope


@private.post("/{unit_id}/review", response_model=RoomEnvelope)
async def start_review(
    unit_id: str, data: StartReview, request: Request, course: str | None = None, user: User = user_auth
) -> RoomEnvelope:
    return await rooms.mutate_room(unit_id, user, get_token(request), data, course)


@private.post("/{unit_id}/llm-grant", response_model=LlmGrant)
async def llm_grant(unit_id: str, course: str | None = None, user: User = user_auth) -> LlmGrant:
    """A short-lived grant for the unit's LLM profiles at llm-ms, after the course and path access check."""
    return await rooms.issue_llm_grant(unit_id, user, course)


router.include_router(private)
