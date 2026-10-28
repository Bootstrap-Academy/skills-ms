"""Curated tracks over courses ("Lernpfade"), in stages of courses and external steps."""

from fastapi import APIRouter, Header, HTTPException, Response

from api.auth import public_auth
from api.schemas.tracks import Track, UserTrack
from api.schemas.user import User
from api.services import learning_plan
from api.services.tracks import TRACKS, list_tracks, public_track, request_language
from api.utils.docs import error_responses

router = APIRouter()

# Tracks are content; the answer only differs by language.
PUBLIC_CACHE = "public, max-age=300"


@router.get("/tracks", response_model=list[Track])
async def get_tracks(response: Response, accept_language: str | None = Header(None)) -> list[Track]:
    """Every track, with titles in the language of `Accept-Language` (de or en, German otherwise)."""
    response.headers["Cache-Control"] = PUBLIC_CACHE
    response.headers["Vary"] = "Accept-Language"
    return list_tracks(request_language(accept_language))


@router.get("/tracks/{track_id}", response_model=UserTrack | Track, responses=error_responses(401, 404, 503))
async def get_track(
    track_id: str, response: Response, user: User | None = public_auth, accept_language: str | None = Header(None)
) -> UserTrack | Track:
    """One track. For a signed-in learner every item carries its `status`: courses by their completion rules,
    external steps as confirmed in the learner's plan."""
    track = TRACKS.get(track_id)
    if track is None:
        raise HTTPException(404, "There is no track with this id")
    language = request_language(accept_language)
    if user is None:
        response.headers["Cache-Control"] = PUBLIC_CACHE
        response.headers["Vary"] = "Accept-Language, Authorization"
        return public_track(track, language)
    response.headers["Cache-Control"] = "private, no-store"
    return await learning_plan.track_for_user(user, track, language)
