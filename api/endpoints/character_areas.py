"""Character areas and their existing skilltrees."""

from fastapi import APIRouter, Response

from api.auth import public_auth
from api.schemas.character_area import CharacterAreas
from api.schemas.skill import SkillTreeResponse
from api.schemas.user import User
from api.services import character_areas


router = APIRouter()


@router.get("/character-areas", response_model=CharacterAreas)
async def list_areas(response: Response) -> CharacterAreas:
    response.headers["Cache-Control"] = "no-store"
    return await character_areas.list_areas()


@router.get("/character-areas/{area_id}/skilltree", response_model=SkillTreeResponse)
async def get_skilltree(area_id: str, response: Response, user: User | None = public_auth) -> SkillTreeResponse:
    response.headers["Cache-Control"] = "private, no-store"
    return await character_areas.get_skilltree(area_id, user)
