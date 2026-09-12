"""Routed learning-room checks on a disposable real SQL database, without network."""

import asyncio
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from pytest import MonkeyPatch

from api import models
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints.rooms import router
from api.schemas.rooms import Catalogue, SaveState
from api.schemas.user import User
from api.services import rooms
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import settings
from api.utils.utc import utcnow


REAL_CHALLENGE_STATUS = rooms.challenge_status
USER = User(id="subject-a", email_verified=True, admin=False)
REF = {"type": "coding", "task_id": str(uuid4()), "subtask_id": str(uuid4())}


def payload(revision: int = 0, **values: Any) -> dict[str, Any]:
    return {"request_id": str(uuid4()), "expected_revision": revision, **values}


@pytest.fixture
def content(monkeypatch: MonkeyPatch) -> Catalogue:
    def unit(uid: str, room: str = "loop-explorer", **values: Any) -> dict[str, Any]:
        return {
            "id": uid,
            "path_id": "python-loops",
            "title": {"de": uid, "en": uid},
            "room": room,
            "content": {"de": {"text": "Synthetic"}},
            "teaches": [],
            "practices": [],
            "requires": [],
            "retired": False,
            **(
                {"completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True}}
                if room != "exercise"
                else {}
            ),
            **values,
        }

    value = Catalogue.parse_obj(
        {
            "paths": [
                {
                    "id": "python-loops",
                    "title": {"de": "Pfad", "en": "Path"},
                    "units": ["intro", "exercise", "later", "retired"],
                }
            ],
            "units": [
                unit("intro", teaches=["loops"]),
                unit("exercise", "exercise", requires=["loops"], practices=["loops"]),
                unit("later"),
                unit("retired", retired=True),
            ],
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {"exercise": REF})
    monkeypatch.setattr(rooms, "load_catalogue", lambda: value)
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    return value


@pytest.fixture
async def room_client(content: Catalogue) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        token = request.headers.get("Authorization")
        if token not in ("Bearer subject-a", "Bearer subject-b"):
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=token.removeprefix("Bearer "), email_verified=True, admin=False)

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = identity
    app.include_router(router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(
        app=app, base_url="http://rooms.synthetic", headers={"Authorization": "Bearer subject-a"}
    ) as client:
        yield client


async def test_gate_auth_private_get_and_no_read_side_effects(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rooms_enabled", False)
    assert (await room_client.get("/rooms/capabilities", headers={"Authorization": ""})).json() == {"enabled": False}
    assert (await room_client.get("/rooms", headers={"Authorization": ""})).status_code == 404
    monkeypatch.setattr(settings, "rooms_enabled", True)
    assert (await room_client.get("/rooms", headers={"Authorization": ""})).status_code == 401
    response = await room_client.get("/rooms")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["next"]["progress"] == {"revision": 0, "state": {}, "status": "new", "result": None}
    assert "completion" not in response.json()["next"]["unit"]
    assert (await room_client.get("/rooms/intro")).status_code == 200
    async with db_context():
        for model in (models.RoomState, models.RoomRequest, models.PurchaseUser):
            assert await db.all(filter_by(model, user_id=USER.id)) == []


async def test_owner_bound_resume_retry_and_conflict(room_client: httpx.AsyncClient) -> None:
    first = payload(state={"step": "predict", "draft": "private-a", "mastery": True})
    saved = await room_client.put("/rooms/intro/state", json=first)
    assert saved.status_code == 200
    assert saved.json()["progress"]["result"] is None
    assert (await room_client.put("/rooms/intro/state", json=first)).json() == saved.json()
    changed = await room_client.put("/rooms/intro/state", json={**first, "state": {"draft": "changed"}})
    assert changed.status_code == 409
    stale = await room_client.put("/rooms/intro/state", json=payload(state={"draft": "stale"}))
    assert stale.status_code == 409
    assert (await room_client.get("/rooms")).json()["next"] == saved.json()
    other = await room_client.get("/rooms/intro", headers={"Authorization": "Bearer subject-b"})
    assert other.json()["progress"]["state"] == {}
    # The same UUID belongs to another identity's independent receipt namespace.
    assert (
        await room_client.put("/rooms/intro/state", json=first, headers={"Authorization": "Bearer subject-b"})
    ).status_code == 200
    async with db_context():
        assert len(await db.all(filter_by(models.RoomRequest, user_id=USER.id))) == 1
        export = await export_user_data(USER.id)
        assert len(export.room_states) == len(export.room_requests) == 1
        assert "subject-b" not in export.json()


async def test_intro_prerequisite_server_answer_and_neutral_skip(room_client: httpx.AsyncClient) -> None:
    assert (await room_client.get("/rooms/exercise")).status_code == 403
    # A GET 'after' neither completes the introduction nor bypasses its prerequisite.
    assert (await room_client.get("/rooms?after=intro")).json()["next"]["unit"]["id"] == "later"
    wrong = await room_client.post("/rooms/intro/complete", json=payload(action="complete", answer={"answer": True}))
    assert wrong.status_code == 422
    complete = payload(action="complete", answer={"answer": 6})
    result = await room_client.post("/rooms/intro/complete", json=complete)
    assert result.json()["progress"]["result"] == {"kind": "introduced"}
    assert (await room_client.post("/rooms/intro/complete", json=complete)).json() == result.json()
    assert (await room_client.get("/rooms")).json()["next"]["unit"]["id"] == "exercise"
    assert (await room_client.post("/rooms/exercise/complete", json=payload(action="skip"))).status_code == 403
    skipped = await room_client.post("/rooms/later/complete", json=payload(action="skip"))
    assert skipped.json()["progress"]["status"] == "skipped"
    assert skipped.json()["progress"]["result"] is None
    assert (await room_client.get("/rooms?after=later")).json()["next"] is None
    assert (await room_client.get("/rooms?after=foreign-unit")).status_code == 404
    async with db_context():
        for model in (
            models.XP,
            models.XPOperation,
            models.CourseAccess,
            models.LectureProgress,
            models.CoursePurchase,
        ):
            assert await db.all(filter_by(model, user_id=USER.id)) == []


async def test_challenge_completion_needs_server_proof_and_preserves_retry(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await room_client.post("/rooms/intro/complete", json=payload(action="skip"))
    attempt = payload(action="complete", answer={"mastery": True, "solved": True})
    assert (await room_client.post("/rooms/exercise/complete", json=attempt)).status_code == 409
    checker = AsyncMock(return_value=True)
    monkeypatch.setattr(rooms, "challenge_status", checker)
    finished = await room_client.post("/rooms/exercise/complete", json=attempt)
    assert finished.json()["progress"]["result"] == {"kind": "solved"}
    assert checker.await_args is not None
    assert checker.await_args.args[1].id == USER.id
    assert checker.await_args.args[2] == USER.id
    checker.reset_mock()
    assert (await room_client.post("/rooms/exercise/complete", json=attempt)).json() == finished.json()
    checker.assert_not_awaited()


async def test_retired_missing_mapping_and_failure_do_not_fake_path_end(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await room_client.post("/rooms/intro/complete", json=payload(action="skip"))
    assert (await room_client.get("/rooms/retired")).status_code == 404
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    assert (await room_client.get("/rooms/exercise")).status_code == 404
    assert (await room_client.get("/rooms")).json()["next"]["unit"]["id"] == "later"
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {"exercise": REF})
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(side_effect=HTTPException(503, "Synthetic outage")))
    assert (await room_client.get("/rooms")).status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"expected_revision": True},
        {"user_id": "subject-b"},
        {"result": {"kind": "solved"}},
        {"state": []},
        {"state": {"code": "x" * 65537}},
        {"expected_revision": -1},
    ],
)
async def test_reject_untrusted_mutation_fields(room_client: httpx.AsyncClient, change: dict[str, Any]) -> None:
    assert (await room_client.put("/rooms/intro/state", json={**payload(state={}), **change})).status_code == 422


async def test_erasure_removes_private_data_and_late_retry_cannot_recreate(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    first = payload(state={"code": "private"})
    assert (await room_client.put("/rooms/intro/state", json=first)).status_code == 200
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await delete_user_data(USER.id)
    assert (await room_client.put("/rooms/intro/state", json=first)).status_code == 401
    assert (await room_client.get("/rooms")).status_code == 401
    async with db_context():
        export = await export_user_data(USER.id)
        assert export.room_states == export.room_requests == []


async def test_transaction_rollback_removes_both_state_and_retry_receipt(content: Catalogue) -> None:
    async with db_context():
        await db.add(models.PurchaseUser(user_id=USER.id, deleted=False))
    data = SaveState.parse_obj(payload(state={"draft": "pending"}))
    with pytest.raises(RuntimeError):
        async with db_context():
            await rooms.mutate_room("intro", USER, USER.id, data)
            raise RuntimeError("Synthetic lost transaction")
    async with db_context():
        assert await db.all(filter_by(models.RoomState, user_id=USER.id)) == []
        assert await db.all(filter_by(models.RoomRequest, user_id=USER.id)) == []
        assert (await rooms.mutate_room("intro", USER, USER.id, data)).progress.revision == 1


@pytest.mark.parametrize(
    "exercise_type,resource,wire_type",
    [
        ("coding", "coding_challenges", "CODING_CHALLENGE"),
        ("matching", "matchings", "MATCHING"),
        ("multiple_choice", "multiple_choice", "MULTIPLE_CHOICE_QUESTION"),
    ],
)
async def test_challenge_check_is_exact_get_current_bearer_and_configured_origin(
    content: Catalogue, monkeypatch: MonkeyPatch, exercise_type: str, resource: str, wire_type: str
) -> None:
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {"exercise": {**REF, "type": exercise_type}})
    monkeypatch.setattr(settings, "challenges_url", "http://challenges.synthetic/base")
    unit = rooms.catalogue().units[1]
    observed: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(
            200,
            json={
                "id": REF["subtask_id"],
                "task_id": REF["task_id"],
                "type": wire_type,
                "creator": "central-content",
                "solved": True,
                "enabled": True,
                "retired": False,
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(remote))
    )
    assert await REAL_CHALLENGE_STATUS(unit, USER, "current-subject-token") is True
    assert len(observed) == 1 and observed[0].method == "GET"
    assert (
        str(observed[0].url)
        == f"http://challenges.synthetic/base/tasks/{REF['task_id']}/{resource}/{REF['subtask_id']}"
    )
    assert observed[0].headers["Authorization"] == "Bearer current-subject-token"


@pytest.mark.parametrize(
    "status,overrides,expected",
    [
        (401, {}, 401),
        (403, {}, 404),
        (404, {}, 404),
        (500, {}, 503),
        (302, {}, 503),
        (200, {"enabled": False}, 404),
        (200, {"retired": True}, 404),
        (200, {"creator": USER.id}, 404),
        (200, {"id": str(uuid4())}, 503),
        (200, {"solved": 1}, 503),
        (200, {"type": "MATCHING"}, 503),
    ],
)
async def test_challenge_refuses_unavailable_or_untrusted_proof(
    content: Catalogue, monkeypatch: MonkeyPatch, status: int, overrides: dict[str, Any], expected: int
) -> None:
    data = {
        "id": REF["subtask_id"],
        "task_id": REF["task_id"],
        "type": "CODING_CHALLENGE",
        "creator": "central-content",
        "solved": True,
        "enabled": True,
        "retired": False,
        **overrides,
    }
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=data, headers={"Location": "http://foreign.invalid"})
    )
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=transport))
    with pytest.raises(HTTPException) as failure:
        await REAL_CHALLENGE_STATUS(rooms.catalogue().units[1], USER, "subject-token")
    assert failure.value.status_code == expected


@pytest.mark.parametrize("initial_revision", [0, 1])
async def test_concurrent_stale_writes_keep_one_revision(content: Catalogue, initial_revision: int) -> None:
    async with db_context():
        await db.add(models.PurchaseUser(user_id=USER.id, deleted=False))
        if initial_revision:
            await db.add(
                models.RoomState(
                    user_id=USER.id,
                    unit_id="intro",
                    revision=initial_revision,
                    state={},
                    status="in_progress",
                    result=None,
                    updated_at=utcnow(),
                )
            )

    async def write(value: int) -> int:
        try:
            async with db_context():
                result = await rooms.mutate_room(
                    "intro", USER, USER.id, SaveState.parse_obj(payload(initial_revision, state={"value": value}))
                )
                return result.progress.revision
        except HTTPException as exc:
            return exc.status_code

    assert sorted(await asyncio.gather(write(1), write(2))) == [initial_revision + 1, 409]
    async with db_context():
        assert len(await db.all(filter_by(models.RoomRequest, user_id=USER.id))) == 1


async def test_autosave_receipts_are_bounded_and_old_retries_still_conflict(room_client: httpx.AsyncClient) -> None:
    requests = []
    results = []
    for revision in range(7):
        request = payload(revision, state={"draft": str(revision)})
        requests.append(request)
        response = await room_client.put("/rooms/intro/state", json=request)
        assert response.status_code == 200
        results.append(response.json())
    assert (await room_client.put("/rooms/intro/state", json=requests[-1])).json() == results[-1]
    assert (await room_client.put("/rooms/intro/state", json=requests[-4])).json() == results[-4]
    assert (await room_client.put("/rooms/intro/state", json=requests[-5])).status_code == 409
    assert (await room_client.get("/rooms/intro")).json() == results[-1]
    async with db_context():
        receipts = await db.all(filter_by(models.RoomRequest, user_id=USER.id))
        assert {receipt.revision for receipt in receipts} == {4, 5, 6, 7}


async def test_unavailable_path_is_distinct_from_completed(
    room_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    await room_client.post("/rooms/intro/complete", json=payload(action="skip"))
    await room_client.post("/rooms/later/complete", json=payload(action="skip"))
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    empty = (await room_client.get("/rooms")).json()
    assert empty["next"] is None and empty["empty_reason"] == "unavailable"
    # Retiring an unavailable exercise removes it from the active path; retired
    # content is never served, and the completed introduction stays completed.
    content.units[1].retired = True
    finished = (await room_client.get("/rooms")).json()
    assert finished["next"] is None and finished["empty_reason"] == "completed"
