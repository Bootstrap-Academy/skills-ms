"""Helpers of the lesson engine's endpoint tests: the catalogue, publishing, the revisions of the unit."""

from typing import Any

import httpx

from api import models
from api.database import db, db_context, filter_by
from tests.engine_documents import UNIT

ORIGIN = "https://lessons.example"
LEARNER = "3f0c9a52-6a4e-4c1b-9d3e-5b8f2a7c1d01"
ADMIN = "8a2d4e61-0b7f-4d9c-a1e3-7c6b5d4f3e02"


def unit(uid: str, **values: Any) -> dict[str, Any]:
    return {
        "id": uid,
        "path_id": "engine-path",
        "title": {"de": uid, "en": uid},
        "room": "guided-lesson",
        "content": {"de": {"text": "Synthetic"}},
        "teaches": [uid],
        "practices": [],
        "requires": [],
        "retired": False,
        "completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True},
        **values,
    }


ENGINE_UNIT = unit(UNIT, room="custom", module_id=UNIT, completion={"kind": "engine"})


async def publish(client: httpx.AsyncClient, body: dict[str, Any], token: str = "admin") -> httpx.Response:
    return await client.post("/engine/admin/revisions", json=body, headers={"Authorization": f"Bearer {token}"})


async def active() -> list[tuple[int, str | None]]:
    async with db_context():
        rows = await db.all(filter_by(models.EngineRevision, unit_id=UNIT))
        return sorted((row.number, row.active_for) for row in rows)
