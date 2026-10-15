"""Lesson engine: routes of stage 1. Behind `engine_enabled`, which stays off until the end-to-end check passes."""

from fastapi import APIRouter, Depends, HTTPException, Response

from api.auth import admin_auth
from api.schemas.engine import RevisionPublished, RevisionRequest
from api.services import engine
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
