"""Lesson engine, PR 2: the admin route for revisions, the API-2 descriptor, the switch and the completion kind."""

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import TypeAdapter, ValidationError
from pytest import MonkeyPatch

from api import models
from api.app import request_validation_error_response
from api.auth import user_auth
from api.database import db, db_context, delete, filter_by
from api.endpoints import engine as engine_endpoints
from api.endpoints import rooms as room_endpoints
from api.exceptions.api_exception import CodedAPIException
from api.schemas.engine import OperationId, ServerDocument
from api.schemas.rooms import Catalogue, CatalogueUnit
from api.schemas.user import User
from api.services import rooms
from api.settings import settings
from tests.engine_documents import PART, SUBSKILL, UNIT, document, request

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


@pytest.fixture
def catalogue(monkeypatch: MonkeyPatch) -> Catalogue:
    value = Catalogue.model_validate(
        {
            "paths": [{"id": "engine-path", "title": {"de": "Pfad", "en": "Path"}, "units": [UNIT, "plain"]}],
            "units": [ENGINE_UNIT, unit("plain")],
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "engine_enabled", True)
    monkeypatch.setattr(settings, "engine_content_origin", ORIGIN)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(rooms, "load_catalogue", lambda: value)
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    return value


@pytest.fixture
async def client(catalogue: Catalogue) -> AsyncIterator[httpx.AsyncClient]:
    async with db_context():
        await db.add(models.LessonModule(id=UNIT, api_version=1, entry_url=f"https://lessons.example/{UNIT}/entry.mjs"))
        await db.add(models.SubSkill(id=SUBSKILL, name="Engine"))
        await db.add(models.SubSkill(id="other-subskill", name="Other"))

    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(http: Request) -> User:
        users = {"Bearer learner": (LEARNER, False), "Bearer admin": (ADMIN, True)}
        token = http.headers.get("Authorization", "")
        if token not in users:
            raise HTTPException(401, "Synthetic missing authority")
        user_id, admin = users[token]
        return User(id=user_id, email_verified=True, admin=admin)

    app = FastAPI()
    app.exception_handler(RequestValidationError)(request_validation_error_response)
    app.add_exception_handler(CodedAPIException, lambda _, exc: exc.response())
    app.dependency_overrides[user_auth.dependency] = identity
    app.include_router(engine_endpoints.router, dependencies=[Depends(session)])
    app.include_router(room_endpoints.router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def publish(client: httpx.AsyncClient, body: dict[str, Any], token: str = "admin") -> httpx.Response:
    return await client.post("/engine/admin/revisions", json=body, headers={"Authorization": f"Bearer {token}"})


async def active() -> list[tuple[int, str | None]]:
    async with db_context():
        rows = await db.all(filter_by(models.EngineRevision, unit_id=UNIT))
        return sorted((row.number, row.active_for) for row in rows)


async def test_publishing_makes_the_newest_content_active_and_the_same_content_again_its_revision(
    client: httpx.AsyncClient,
) -> None:
    first = await publish(client, request())
    assert first.status_code == 201
    body = first.json()
    assert (body["number"], body["active"], body["created"]) == (1, True, True)
    async with db_context():
        row = await db.get(models.EngineRevision, id=body["revision_id"])
        assert row is not None
        assert row.tasks == [{"goalId": "reach", "required": True, "xp": 1, "subskillId": SUBSKILL, "input": "program"}]
        assert (row.module_id, row.server_document) == (UNIT, document())

    # The same content again is the same revision, still active.
    again = await publish(client, request())
    assert (again.status_code, again.json()["revision_id"], again.json()["created"]) == (
        200,
        body["revision_id"],
        False,
    )
    # New content gets the next number and becomes the active revision.
    changed = request(document(parts=(PART, "// second part\n")), package_hash="d" * 64)
    second = await publish(client, changed)
    assert (second.status_code, second.json()["number"]) == (201, 2)
    assert await active() == [(1, None), (2, UNIT)]
    # Publishing the first content again makes revision 1 active again (a rollback).
    back = await publish(client, request())
    assert (back.status_code, back.json()["number"]) == (200, 1)
    assert await active() == [(1, UNIT), (2, None)]


BROKEN: dict[str, tuple[dict[str, Any], int, str]] = {
    "hash that is not the document's": (request(server_sha256="0" * 64), 422, "server_sha256 is not the SHA-256"),
    "hash in capitals": (request(package_hash="A" * 64), 422, "package_hash"),
    "hash that is no hex": (request(evidence_sha256="g" * 64), 422, "evidence_sha256"),
    "entry outside the package": (request(entry_path="../index.html"), 422, "entry_path"),
    "parts that do not match the runner hash": (
        request({**document(), "runner": {**document()["runner"], "sha256": "e" * 64}}),
        422,
        "do not match runner.sha256",
    ),
    "part that is not ASCII": (request(document(parts=("const ä = 1;\n",))), 422, "ASCII"),
    "number that is no integer": (
        request({**document(), "engine": 0.6}, server_sha256="0" * 64),
        422,
        "no canonical form",
    ),
    "document of another unit": (request(document(unit_id="plain"), unit_id=UNIT), 422, "belongs to unit plain"),
    "unknown sub-skill": (request(document(subskill="missing-subskill")), 422, "Unknown sub-skills: missing-subskill"),
    "unit that is not of the engine": (request(document(unit_id="plain")), 404, "no unit of the lesson engine"),
    "unknown unit": (request(document(unit_id="nowhere")), 404, "no unit of the lesson engine"),
    "document beyond a megabyte": (
        request(
            {**document(), "tasks": [{**document()["tasks"][0], "feedback": {"boundary": {"de": "x" * 1_100_000}}}]}
        ),
        413,
        "at most 1048576 are accepted",
    ),
}


@pytest.mark.parametrize("name", BROKEN)
async def test_a_broken_publication_is_refused_and_changes_nothing(client: httpx.AsyncClient, name: str) -> None:
    body, status, detail = BROKEN[name]
    response = await publish(client, body)
    assert response.status_code == status, response.text
    assert detail in response.text
    assert await active() == []


async def test_a_later_revision_keeps_tasks_required_set_xp_and_subskills(client: httpx.AsyncClient) -> None:
    assert (await publish(client, request())).status_code == 201
    for changed in [
        document(xp=2, parts=("// xp\n",)),
        document(subskill="other-subskill", parts=("// subskill\n",)),
        document(goal="other", parts=("// goal\n",)),
    ]:
        response = await publish(client, request(changed))
        assert response.status_code == 409, response.text
        assert "keeps the task ids, the required tasks, their XP and their sub-skills" in response.text
    assert await active() == [(1, UNIT)]


async def test_publishing_needs_an_admin_the_switch_and_the_module_row(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    assert (await publish(client, request(), token="learner")).status_code == 403
    async with db_context():
        await db.exec(delete(models.LessonModule).where(models.LessonModule.id == UNIT))
    missing = await publish(client, request())
    assert (missing.status_code, "Register the lesson module engine-unit" in missing.text) == (409, True)
    monkeypatch.setattr(settings, "engine_enabled", False)
    assert (await publish(client, request())).status_code == 404
    assert await active() == []


async def test_the_unit_carries_the_package_of_its_active_revision(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    learner = {"Authorization": "Bearer learner"}
    # Without an active revision the lesson cannot be loaded yet.
    assert (await client.get(f"/rooms/{UNIT}", headers=learner)).status_code == 503
    revision_id = (await publish(client, request())).json()["revision_id"]
    room = (await client.get(f"/rooms/{UNIT}", headers=learner)).json()
    base = f"{ORIGIN}/packages/{'a' * 64}/"
    assert room["unit"]["module"] == {
        "id": UNIT,
        "api_version": 2,
        "entry_url": base + "index.html",
        "manifest_url": base + "manifest.json",
        "manifest_hash": "b" * 64,
        "package_hash": "a" * 64,
        "revision_id": revision_id,
    }
    assert room["unit"]["completion_kind"] == "engine"
    # Without a content origin there is nowhere to load it from.
    monkeypatch.setattr(settings, "engine_content_origin", None)
    assert (await client.get(f"/rooms/{UNIT}", headers=learner)).status_code == 503
    # While the engine is switched off, its unit is unavailable like a retired one; other units are not.
    monkeypatch.setattr(settings, "engine_enabled", False)
    assert (await client.get(f"/rooms/{UNIT}", headers=learner)).status_code == 404
    assert (await client.get("/rooms/plain", headers=learner)).status_code == 200


async def test_a_client_cannot_complete_skip_or_repeat_a_unit_of_the_engine(client: httpx.AsyncClient) -> None:
    await publish(client, request())
    learner = {"Authorization": "Bearer learner"}

    def body(**values: Any) -> dict[str, Any]:
        return {"request_id": str(uuid4()), "expected_revision": 0, **values}

    completed = await client.post(f"/rooms/{UNIT}/complete", json=body(action="complete"), headers=learner)
    assert (completed.status_code, "completed by the server" in completed.text) == (409, True)
    skipped = await client.post(f"/rooms/{UNIT}/complete", json=body(action="skip"), headers=learner)
    assert skipped.status_code == 403
    repeated = await client.post(f"/rooms/{UNIT}/review", json=body(), headers=learner)
    assert (repeated.status_code, "no practice round yet" in repeated.text) == (409, True)
    async with db_context():
        assert not await db.exists(filter_by(models.RoomState, user_id=LEARNER))


@pytest.mark.parametrize(
    "values, message",
    [
        ({"room": "guided-lesson"}, "A lesson of the engine is a custom activity"),
        ({"milestone": {"skill_id": SUBSKILL, "xp": 5}}, "earns its XP per task"),
        ({"module_id": None}, "A custom activity requires a registered module ID"),
    ],
)
def test_the_catalogue_refuses_a_unit_of_the_engine_that_is_not_a_custom_activity_or_has_a_milestone(
    values: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        CatalogueUnit.model_validate({**ENGINE_UNIT, **values})


def test_an_operation_id_of_the_protocol_has_up_to_100_characters() -> None:
    ids = TypeAdapter(OperationId)
    assert ids.validate_python("a" * 100) == "a" * 100
    assert ids.validate_python("start-1792050000000-3") == "start-1792050000000-3"
    for refused in ["", "a" * 101, "start/1", "start 1"]:
        with pytest.raises(ValidationError):
            ids.validate_python(refused)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["tasks"][0].update(xp="1"),
        lambda d: d["tasks"][0].update(required=1),
        lambda d: d["bandRule"].update(initial="hard"),
        lambda d: d["tasks"][0]["practice"].pop("plus"),
        lambda d: d["tasks"][0].update(required=False),
        lambda d: d.update(extra=True),
        lambda d: d.update(formatVersion=2),
        lambda d: d["runner"]["parts"].extend({"name": f"p-{i}.txt", "content": ""} for i in range(10)),
        lambda d: d["runner"]["parts"][0].update(name="../server.txt"),
    ],
)
def test_the_server_document_is_read_strictly(change: Any) -> None:
    value = document()
    change(value)
    with pytest.raises(ValidationError):
        ServerDocument.model_validate(value)
