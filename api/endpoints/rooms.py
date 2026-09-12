"""Private, curated learning rooms shared by current and future clients."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.auth import get_token, require_verified_email, user_auth
from api.schemas.rooms import Complete, RoomEnvelope, Rooms, SaveState
from api.schemas.user import User
from api.services import rooms
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
    request: Request, path: str = "python-loops", after: str | None = None, user: User = user_auth
) -> Rooms:
    return await rooms.next_room(user, get_token(request), path, after)


@private.get("/{unit_id}", response_model=RoomEnvelope)
async def get_room(unit_id: str, request: Request, user: User = user_auth) -> RoomEnvelope:
    return await rooms.get_room(unit_id, user, get_token(request))


@private.put("/{unit_id}/state", response_model=RoomEnvelope)
async def save_state(unit_id: str, data: SaveState, request: Request, user: User = user_auth) -> RoomEnvelope:
    return await rooms.mutate_room(unit_id, user, get_token(request), data)


@private.post("/{unit_id}/complete", response_model=RoomEnvelope)
async def complete(unit_id: str, data: Complete, request: Request, user: User = user_auth) -> RoomEnvelope:
    return await rooms.mutate_room(unit_id, user, get_token(request), data)


router.include_router(private)
