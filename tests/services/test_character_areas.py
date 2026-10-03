"""Area projections preserve the global tree and can split explicit future areas."""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch

from api import models
from api.database import db, db_context
from api.endpoints.skill import list_root_skills
from api.schemas.user import User
from api.services.character_areas import get_skilltree, list_areas
from api.settings import settings


async def test_default_it_tree_equals_existing_tree_and_can_add_an_area(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    async with db_context():
        for identity, row, column in (("python", 2, 3), ("life", 4, 5)):
            await db.add(
                models.RootSkill(
                    id=identity,
                    name=identity,
                    row=row,
                    column=column,
                    sub_tree_rows=8,
                    sub_tree_columns=9,
                    sub_skills=[],
                    dependencies=[],
                    dependents=[],
                )
            )
        await db.add(models.TreeSettings(rows=40, columns=30))
    async with db_context():
        before = await list_root_skills.__wrapped__(user=None)  # type: ignore
        areas = await list_areas()
        assert areas.areas[0].id == "it" and areas.areas[0].root_skill_ids == ["life", "python"]
        it_tree = await get_skilltree("it", None)
        assert it_tree == before
    path = tmp_path / "areas.json"
    path.write_text(
        json.dumps(
            {
                "areas": [
                    {"id": "it", "title": {"de": "IT", "en": "IT"}, "include_unassigned_roots": True},
                    {
                        "id": "life",
                        "title": {"de": "Leben", "en": "Life"},
                        "root_skill_ids": ["life"],
                        "rows": 10,
                        "columns": 12,
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(settings, "character_areas", path)
    async with db_context():
        it_tree = await get_skilltree("it", User(id="synthetic", email_verified=True, admin=False))
        life_tree = await get_skilltree("life", None)
        assert [(root.id, root.row, root.column) for root in it_tree.skills] == [("python", 2, 3)]
        assert [(root.id, root.row, root.column) for root in life_tree.skills] == [("life", 4, 5)]
        assert life_tree.rows == 10 and life_tree.columns == 12
        after = await list_root_skills.__wrapped__(user=None)  # type: ignore
        assert after == before
        with pytest.raises(HTTPException) as error:
            await get_skilltree("missing", None)
        assert error.value.status_code == 404


async def test_unknown_configured_root_does_not_silently_disappear(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    path = tmp_path / "areas.json"
    path.write_text(
        json.dumps({"areas": [{"id": "it", "title": {"de": "IT", "en": "IT"}, "root_skill_ids": ["missing"]}]})
    )
    monkeypatch.setattr(settings, "character_areas", path)
    async with db_context():
        with pytest.raises(HTTPException) as error:
            await list_areas()
        assert error.value.status_code == 503
        assert await db.get(models.TreeSettings) is None
