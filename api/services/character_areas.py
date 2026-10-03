"""Read-only area projections; the legacy global tree remains unchanged."""

from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from api import models
from api.database import db, select
from api.schemas.character_area import CharacterArea, CharacterAreaCatalogue, CharacterAreas
from api.schemas.skill import RootSkillResponse, SkillTreeResponse
from api.schemas.user import User
from api.settings import settings


@lru_cache(maxsize=4)
def load_areas(path: Path) -> CharacterAreaCatalogue:
    try:
        return CharacterAreaCatalogue.parse_raw(path.read_text())
    except (OSError, ValueError):
        raise HTTPException(503, "Deine Lernbereiche können gerade nicht geladen werden.") from None


async def list_areas() -> CharacterAreas:
    definition = load_areas(settings.character_areas)
    roots = set(await db.all(select(models.RootSkill.id)))
    assigned = {root for area in definition.areas for root in area.root_skill_ids}
    if assigned - roots:
        raise HTTPException(503, "Deine Lernbereiche können gerade nicht geladen werden.")
    return CharacterAreas(
        areas=[
            CharacterArea(
                id=area.id,
                title=area.title,
                root_skill_ids=sorted(
                    set(area.root_skill_ids) | (roots - assigned if area.include_unassigned_roots else set())
                ),
            )
            for area in definition.areas
        ]
    )


async def get_skilltree(area_id: str, user: User | None) -> SkillTreeResponse:
    areas = await list_areas()
    area = next((item for item in areas.areas if item.id == area_id), None)
    if area is None:
        raise HTTPException(404, "Diesen Lernbereich gibt es nicht.")
    definition = next(item for item in load_areas(settings.character_areas).areas if item.id == area_id)
    tree_settings = await db.get(models.TreeSettings)
    root_ids = set(area.root_skill_ids)
    bookmarks = (
        set(
            await db.all(
                select(models.SubSkillBookmark.root_skill_id).where(models.SubSkillBookmark.user_id == user.id)
            )
        )
        if user is not None
        else set()
    )
    root_query = select(models.RootSkill)
    if len(areas.areas) != 1 or not definition.include_unassigned_roots:
        root_query = root_query.where(models.RootSkill.id.in_(root_ids))
    roots = await db.all(root_query)
    return SkillTreeResponse(
        rows=definition.rows or (tree_settings.rows if tree_settings else 20),
        columns=definition.columns or (tree_settings.columns if tree_settings else 20),
        skills=[
            RootSkillResponse(
                **{
                    **root.serialize,
                    "dependencies": [dependency.id for dependency in root.dependencies if dependency.id in root_ids],
                    "dependents": [dependent.id for dependent in root.dependents if dependent.id in root_ids],
                },
                is_bookmarked=(root.id in bookmarks) if user is not None else None,
            )
            for root in roots
        ],
    )
