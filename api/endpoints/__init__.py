from typing import Any

from fastapi import APIRouter

from . import bookmarks, character_areas, course, curriculum, daily_limit, learning, lesson_assets, rooms, skill, xp
from .internal import INTERNAL_ROUTERS
from ..auth import internal_auth

ROUTER = APIRouter()
TAGS: list[dict[str, Any]] = []

for module in [skill, bookmarks, course, xp, learning, rooms, curriculum, daily_limit, character_areas, lesson_assets]:
    name = module.__name__.split(".")[-1]
    router = APIRouter(tags=[name])
    router.include_router(module.router)
    ROUTER.include_router(router)

    TAGS.append({"name": name, "description": module.__doc__ or ""})

TAGS.append({"name": "internal", "description": "Internal endpoints"})

for r in INTERNAL_ROUTERS:
    router = APIRouter(prefix="/_internal", tags=["internal"], dependencies=[internal_auth])
    router.include_router(r)
    ROUTER.include_router(router)
