"""Routed learning-room checks on a disposable real SQL database, without network."""

import asyncio
from datetime import timedelta
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
from api.endpoints import course as course_endpoints
from api.endpoints.rooms import router
from api.schemas.course import Course
from api.schemas.rooms import Catalogue, CataloguePath, SaveState
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
    app.include_router(course_endpoints.router, dependencies=[Depends(session)])
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
    assert response.json()["next"]["progress"] == {
        "revision": 0,
        "state": {},
        "status": "new",
        "result": None,
        "review_id": None,
    }
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


async def finish_pilot(client: httpx.AsyncClient, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))
    for uid in ("intro", "exercise", "later"):
        response = await client.post(f"/rooms/{uid}/complete", json=payload(action="complete", answer={"answer": 6}))
        assert response.status_code == 200


async def test_continuous_paths_then_repeat_without_get_writes(
    room_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    other = content.units[0].copy(deep=True, update={"id": "other", "path_id": "math"})
    content.units.append(other)
    content.paths.append(
        CataloguePath.parse_obj({"id": "math", "title": {"de": "Mathe", "en": "Math"}, "units": ["other"]})
    )
    await finish_pilot(room_client, monkeypatch)
    assert (await room_client.get("/rooms?after=later")).json()["next"] is None
    selected = (await room_client.get("/rooms?continuous=true&after=later")).json()
    assert selected["path"]["id"] == "math" and selected["next"]["unit"]["id"] == "other"
    assert selected["next"]["review_available"] is False
    assert (
        await room_client.post("/rooms/other/complete", json=payload(action="complete", answer={"answer": 6}))
    ).status_code == 200
    async with db_context():
        before = [(row.unit_id, row.revision) for row in await db.all(filter_by(models.RoomState, user_id=USER.id))]
    selected = (await room_client.get("/rooms?continuous=true&path=math&after=other")).json()
    assert selected["path"]["id"] == "python-loops"
    assert selected["next"]["unit"]["id"] == "intro" and selected["next"]["review_available"] is True
    async with db_context():
        after = [(row.unit_id, row.revision) for row in await db.all(filter_by(models.RoomState, user_id=USER.id))]
        assert before == after
        assert await db.all(filter_by(models.XP, user_id=USER.id)) == []


@pytest.mark.parametrize("originally_skipped", [False, True])
async def test_review_keeps_original_achievement_and_private_resume(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, originally_skipped: bool
) -> None:
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    await finish_pilot(room_client, monkeypatch)
    if originally_skipped:
        async with db_context():
            row = await db.get(models.RoomState, user_id=USER.id, unit_id="intro")
            assert row is not None
            row.status, row.result = "skipped", None
    start = payload(1)
    opened = await room_client.post("/rooms/intro/review", json=start)
    assert opened.status_code == 200
    value = opened.json()["progress"]
    assert value == {
        "revision": 2,
        "state": {},
        "status": "in_progress",
        "result": None,
        "review_id": start["request_id"],
    }
    assert (await room_client.post("/rooms/intro/review", json=start)).json() == opened.json()
    assert (await room_client.post("/rooms/intro/review", json=payload(2))).status_code == 409
    assert (await room_client.put("/rooms/intro/state", json=payload(2, state={"stale": True}))).status_code == 409
    saved = await room_client.put(
        "/rooms/intro/state", json=payload(2, review_id=start["request_id"], state={"step": 2})
    )
    assert saved.status_code == 200
    resumed = (await room_client.get("/rooms?continuous=true")).json()["next"]
    assert resumed["progress"] == saved.json()["progress"] and resumed["review_available"] is False
    assert (await room_client.get("/rooms/exercise")).status_code == 200  # prerequisite survives the repeat
    wrong = payload(3, review_id=start["request_id"], action="complete", answer={"answer": 5})
    assert (await room_client.post("/rooms/intro/complete", json=wrong)).status_code == 422
    completed = await room_client.post(
        "/rooms/intro/complete", json=payload(3, review_id=start["request_id"], action="complete", answer={"answer": 6})
    )
    assert completed.status_code == 200 and completed.json()["progress"]["status"] == "completed"
    assert completed.json()["progress"]["result"] == {"kind": "introduced"}
    assert (await room_client.get("/rooms/intro")).json()["progress"] == completed.json()["progress"]
    next_room = (await room_client.get("/rooms?continuous=true&after=intro")).json()["next"]
    assert next_room["unit"]["id"] == "exercise" and next_room["review_available"] is True
    async with db_context():
        row = await db.get(models.RoomState, user_id=USER.id, unit_id="intro")
        assert row is not None
        assert row.status == ("skipped" if originally_skipped else "completed")
        assert row.result == (None if originally_skipped else {"kind": "introduced"})
        assert row.review_id == start["request_id"] and row.review_status == "completed"
        exported = await export_user_data(USER.id)
        assert any(item["review_id"] == start["request_id"] for item in exported.room_states)
        await delete_user_data(USER.id)
    assert (await room_client.post("/rooms/intro/review", json=start)).status_code == 401


async def test_repeat_completion_requires_its_own_new_attempt(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await finish_pilot(room_client, monkeypatch)
    start = payload(1)
    assert (await room_client.post("/rooms/exercise/review", json=start)).status_code == 200
    # The old solved=true is deliberately still returned by challenge_status.
    proof = AsyncMock(return_value=False)
    monkeypatch.setattr(rooms, "review_attempt_solved", proof)
    body = payload(2, action="complete", review_id=start["request_id"])
    assert (await room_client.post("/rooms/exercise/complete", json=body)).status_code == 409
    assert proof.await_count == 0
    body["attempt_id"] = str(uuid4())
    assert (await room_client.post("/rooms/exercise/complete", json=body)).status_code == 409
    proof.return_value = True
    result = await room_client.post("/rooms/exercise/complete", json=body)
    assert result.status_code == 200 and result.json()["progress"]["result"] == {"kind": "solved"}
    assert (await room_client.post("/rooms/exercise/complete", json=body)).json() == result.json()
    assert proof.await_count == 2  # replay uses the receipt, not another proof or charge


@pytest.mark.parametrize("kind", ["multiple_choice", "matching", "coding"])
async def test_repeat_proof_binds_attempt_owner_exercise_time_and_verdict(
    content: Catalogue, monkeypatch: MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {"exercise": {**REF, "type": kind}})
    monkeypatch.setattr(settings, "challenges_url", "http://challenges.synthetic")
    started = utcnow()
    row = models.RoomState(review_started_at=started)
    attempt = uuid4()
    data = {
        "id": str(attempt),
        "task_id": REF["task_id"],
        "subtask_id": REF["subtask_id"],
        "user_id": USER.id,
        "creator": USER.id,
        "solved": True,
        "result": {"verdict": "OK"},
        "created_at": (started + timedelta(seconds=1)).isoformat(),
        "creation_timestamp": (started + timedelta(seconds=1)).isoformat(),
    }
    original = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def remote(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[data] if kind == "coding" else data)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(remote))
    )
    unit = rooms.catalogue().units[1]
    assert await rooms.review_attempt_solved(unit, USER, "own-token", row, attempt) is True
    original_data = dict(data)
    for key, wrong in [
        ("id", str(uuid4())),
        ("subtask_id", str(uuid4())),
        ("creator" if kind == "coding" else "user_id", "another-owner"),
        ("creation_timestamp" if kind == "coding" else "created_at", (started - timedelta(seconds=1)).isoformat()),
        ("result" if kind == "coding" else "solved", {"verdict": "WRONG_ANSWER"} if kind == "coding" else False),
    ]:
        data.clear()
        data.update(original_data)
        data[key] = wrong
        assert await rooms.review_attempt_solved(unit, USER, "own-token", row, attempt) is False
    assert all(
        request.method == "GET" and request.headers["Authorization"] == "Bearer own-token" for request in requests
    )
    suffix = "/submissions" if kind == "coding" else f"/attempts/{attempt}"
    assert str(requests[0].url).endswith(suffix)


def linked_course(monkeypatch: MonkeyPatch, price: int = 0) -> Course:
    value = Course(
        id="synthetic-course",
        title="Course",
        description=None,
        category=None,
        language="de",
        image=None,
        authors=[],
        price=price,
        learning_goals=[],
        requirements=[],
        last_update=0,
        learning_path_id="python-loops",
    )
    monkeypatch.setattr(rooms, "COURSES", {value.id: value})
    monkeypatch.setattr(course_endpoints, "COURSES", {value.id: value})
    return value


async def test_course_outline_and_stream_share_private_progress_without_video_or_new_rewards(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    response = await room_client.get(f"/courses/{course.id}/learning")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    outline = response.json()
    assert [unit["id"] for unit in outline["units"]] == ["intro", "exercise", "later"]
    assert [unit["available"] for unit in outline["units"]] == [True, False, True]
    assert not outline["completed"] and outline["next"]["unit"]["id"] == "intro"
    assert (await room_client.get(f"/courses/{course.id}/next_unseen")).status_code == 404
    assert course.sections == []
    assert course.summary(set()).completed is None  # videos never assert completion of the linked path
    saved = await room_client.put("/rooms/intro/state", json=payload(state={"draft": "only-a"}))
    outline = (await room_client.get(f"/courses/{course.id}/learning")).json()
    assert outline["units"][0]["status"] == "in_progress" and outline["next"] == saved.json()
    other = await room_client.get(f"/courses/{course.id}/learning", headers={"Authorization": "Bearer subject-b"})
    assert other.json()["next"]["progress"]["state"] == {}
    assert "only-a" not in other.text
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))
    for uid, revision in (("intro", 1), ("exercise", 0), ("later", 0)):
        assert (
            await room_client.post(
                f"/rooms/{uid}/complete", json=payload(revision, action="complete", answer={"answer": 6})
            )
        ).status_code == 200
    finished = (await room_client.get(f"/courses/{course.id}/learning")).json()
    assert finished["completed"] and finished["next"] is None and finished["empty_reason"] == "completed"
    assert [unit["result"]["kind"] for unit in finished["units"]] == ["introduced", "solved", "introduced"]
    async with db_context():
        assert await rooms.course_completions(USER) == {"python-loops": True}
        for model in (models.XP, models.LectureProgress, models.CourseAccess, models.LastWatch):
            assert await db.all(filter_by(model, user_id=USER.id)) == []
    # Starting a new review keeps the original course completion.
    assert (await room_client.post("/rooms/intro/review", json=payload(2))).status_code == 200
    assert (await room_client.get(f"/courses/{course.id}/learning")).json()["completed"] is True


@pytest.mark.parametrize("admission", ["purchased", "historical", "premium"])
async def test_paid_path_cannot_bypass_course_admission_and_keeps_existing_rights(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, admission: str
) -> None:
    course = linked_course(monkeypatch, price=100)
    premium = AsyncMock(return_value=False)
    monkeypatch.setattr(rooms, "has_premium", premium)
    monkeypatch.setattr(course_endpoints, "has_premium", premium)
    for route in (f"/courses/{course.id}/learning", "/rooms/intro", "/rooms?continuous=true"):
        assert (await room_client.get(route)).status_code == 403
    assert (await room_client.put("/rooms/intro/state", json=payload(state={}))).status_code == 403
    assert (await room_client.post("/rooms/intro/complete", json=payload(action="skip"))).status_code == 403
    if admission == "premium":
        premium.return_value = True
    else:
        async with db_context():
            if admission == "purchased":
                await db.add(models.CourseAccess(user_id=USER.id, course_id=course.id))
            else:
                await models.LastWatch.update(USER.id, course.id)
    for route in (f"/courses/{course.id}/learning", "/rooms/intro", "/rooms?continuous=true"):
        assert (await room_client.get(route)).status_code == 200


async def test_selected_direction_resumes_own_work_then_other_chapters_and_repeats(
    room_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    content.paths[0].direction_id = "python"
    for path_id, direction in (("math", "math"), ("python-next", "python")):
        other = content.units[0].copy(deep=True, update={"id": path_id, "path_id": path_id})
        content.units.append(other)
        content.paths.append(CataloguePath(id=path_id, title=other.title, units=[path_id], direction_id=direction))
    assert (await room_client.get("/rooms?continuous=true&direction=math")).status_code == 404
    assert (
        await room_client.put("/rooms/python-next/state", json=payload(state={"draft": "continue me"}))
    ).status_code == 200
    resumed = (await room_client.get("/rooms?continuous=true")).json()
    assert resumed["next"]["unit"]["id"] == "python-next"
    assert {path["id"] for path in resumed["paths"]} == {"python-loops", "python-next", "math"}
    # An explicit next request leaves that draft intact and continues the same subject.
    following = (await room_client.get("/rooms?continuous=true&path=python-next&after=python-next")).json()
    assert following["next"]["unit"]["id"] == "intro"
    await finish_pilot(room_client, monkeypatch)
    assert (
        await room_client.post("/rooms/python-next/complete", json=payload(1, action="complete", answer={"answer": 6}))
    ).status_code == 200
    repeated = (await room_client.get("/rooms?continuous=true&path=python-next&after=python-next")).json()
    assert repeated["next"]["review_available"] and repeated["next"]["unit"]["id"] == "intro"
    assert repeated["path"]["direction_id"] == "python"
    assert (await room_client.get("/rooms/math")).json()["progress"]["status"] == "new"


async def test_guided_lesson_uses_exact_server_answers_without_claiming_solved_skill(
    room_client: httpx.AsyncClient, content: Catalogue
) -> None:
    lesson = content.units[0]
    lesson.room = "guided-lesson"
    assert lesson.completion is not None
    lesson.completion.answer = {"checks": {"variable": "name", "amount": 3}}
    wrong = payload(action="complete", answer={"checks": {"variable": "name", "amount": True}})
    assert (await room_client.post("/rooms/intro/complete", json=wrong)).status_code == 422
    response = await room_client.post(
        "/rooms/intro/complete", json=payload(action="complete", answer=lesson.completion.answer)
    )
    assert response.status_code == 200 and response.json()["progress"]["result"] == {"kind": "introduced"}


async def test_course_unit_choice_is_read_only_and_does_not_introduce_missing_concepts(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    assert (await room_client.get("/rooms/exercise")).status_code == 403
    response = await room_client.get(f"/rooms?course={course.id}&unit=exercise&continuous=true")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json()["next"]["unit"]["id"] == "exercise"
    assert response.json()["next"]["progress"]["status"] == "new"
    outline = (await room_client.get(f"/courses/{course.id}/learning")).json()
    assert outline["units"][1]["available"] is False and outline["units"][1]["selectable"] is True
    assert all(unit["status"] == "new" for unit in outline["units"])
    async with db_context():
        assert await rooms.read_states(USER.id) == {}
        assert rooms.introduced_concepts(rooms.catalogue(), {}) == set()
        for model in (models.PurchaseUser, models.RoomRequest, models.XP, models.XPOperation):
            assert await db.all(filter_by(model, user_id=USER.id)) == []


async def test_course_drafts_and_request_retries_keep_exact_context_and_owner(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    duplicate = course.copy(update={"id": "another-course"})
    monkeypatch.setattr(rooms, "COURSES", {course.id: course, duplicate.id: duplicate})
    query = f"?course={course.id}"
    body = payload(state={"draft": "later lesson, private"})
    response = await room_client.put(f"/rooms/exercise/state{query}", json=body)
    assert response.status_code == 200
    assert (await room_client.get(f"/rooms/exercise{query}")).json() == response.json()
    assert (await room_client.put(f"/rooms/exercise/state{query}", json=body)).json() == response.json()
    assert (await room_client.put(f"/rooms/exercise/state?course={duplicate.id}", json=body)).status_code == 409
    assert (await room_client.get("/rooms/exercise")).status_code == 403
    other = await room_client.get(f"/rooms/exercise{query}", headers={"Authorization": "Bearer subject-b"})
    assert other.json()["progress"]["state"] == {}
    outline = (await room_client.get(f"/courses/{course.id}/learning")).json()
    assert outline["next"]["unit"]["id"] == "exercise" and outline["completed"] is False
    assert outline["units"][0]["status"] == "new"
    async with db_context():
        states = await rooms.read_states(USER.id)
        assert set(states) == {"exercise"} and rooms.introduced_concepts(rooms.catalogue(), states) == set()
        assert len(await db.all(filter_by(models.RoomRequest, user_id=USER.id))) == 1


async def test_course_completion_and_repeat_still_require_real_results(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    query = f"?course={course.id}"
    first = payload(action="complete", answer={"solved": True})
    assert (await room_client.post(f"/rooms/exercise/complete{query}", json=first)).status_code == 409
    assert (await room_client.post(f"/rooms/exercise/complete{query}", json=payload(action="skip"))).status_code == 403
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))
    finished = await room_client.post(f"/rooms/exercise/complete{query}", json=first)
    assert finished.status_code == 200 and finished.json()["progress"]["result"] == {"kind": "solved"}
    assert (await room_client.post(f"/rooms/exercise/complete{query}", json=first)).json() == finished.json()
    selected = await room_client.get(f"/rooms?course={course.id}&unit=exercise")
    assert selected.json()["next"]["review_available"] is True
    review = payload(1)
    started = await room_client.post(f"/rooms/exercise/review{query}", json=review)
    assert started.status_code == 200
    assert (await room_client.post(f"/rooms/exercise/review{query}", json=review)).json() == started.json()
    current = started.json()["progress"]
    complete = payload(2, review_id=current["review_id"], attempt_id=str(uuid4()), action="complete")
    monkeypatch.setattr(rooms, "review_attempt_solved", AsyncMock(return_value=False))
    assert (await room_client.post(f"/rooms/exercise/complete{query}", json=complete)).status_code == 409
    monkeypatch.setattr(rooms, "review_attempt_solved", AsyncMock(return_value=True))
    assert (await room_client.post(f"/rooms/exercise/complete{query}", json=complete)).status_code == 200
    async with db_context():
        states = await rooms.read_states(USER.id)
        assert set(states) == {"exercise"} and states["exercise"].status == "completed"
        assert not rooms.path_completed(rooms.catalogue(), states, "python-loops")
        for model in (models.XP, models.XPOperation, models.LectureProgress, models.CourseAccess):
            assert await db.all(filter_by(model, user_id=USER.id)) == []


@pytest.mark.parametrize("admission", ["purchased", "historical", "premium"])
async def test_course_navigation_requires_its_exact_paid_course_on_every_request_and_retry(
    room_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, admission: str
) -> None:
    course = linked_course(monkeypatch, price=100)
    # A free course on the same path does not authorize the requested paid course.
    monkeypatch.setattr(
        rooms, "COURSES", {course.id: course, "free-alias": course.copy(update={"id": "free-alias", "price": 0})}
    )
    premium = AsyncMock(return_value=False)
    monkeypatch.setattr(rooms, "has_premium", premium)
    requests = [
        ("GET", f"/rooms?course={course.id}&unit=exercise", None),
        ("GET", f"/rooms?course={course.id}&after=intro&continuous=true", None),
        ("GET", f"/rooms/exercise?course={course.id}", None),
        ("PUT", f"/rooms/exercise/state?course={course.id}", payload(state={})),
        ("POST", f"/rooms/exercise/complete?course={course.id}", payload(action="complete")),
        ("POST", f"/rooms/exercise/review?course={course.id}", payload()),
    ]
    for method, route, body in requests:
        assert (await room_client.request(method, route, json=body)).status_code == 403
    if admission == "premium":
        premium.return_value = True
    else:
        async with db_context():
            if admission == "purchased":
                await db.add(models.CourseAccess(user_id=USER.id, course_id=course.id))
            else:
                await models.LastWatch.update(USER.id, course.id)
    assert (await room_client.get(requests[0][1])).status_code == 200
    saved = await room_client.put(requests[3][1], json=requests[3][2])
    assert saved.status_code == 200
    assert (await room_client.put(requests[3][1], json=requests[3][2])).json() == saved.json()
    # Authority is checked before an exact receipt is replayed.
    premium.return_value = False
    monkeypatch.setattr(rooms, "get_owned_courses", AsyncMock(return_value=set()))
    assert (await room_client.put(requests[3][1], json=requests[3][2])).status_code == 403
    assert (await room_client.get(requests[0][1])).status_code == 403


async def test_course_navigation_refuses_other_paths_retired_and_unconfigured_units(
    room_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    other = content.units[0].copy(deep=True, update={"id": "foreign", "path_id": "foreign"})
    content.units.append(other)
    content.paths.append(CataloguePath(id="foreign", title=other.title, units=["foreign"]))
    for route, status in [
        ("/rooms?unit=exercise", 422),
        (f"/rooms?course={course.id}&unit=exercise&after=intro", 422),
        ("/rooms?course=unknown&unit=exercise", 404),
        (f"/rooms?course={course.id}&path=foreign&unit=foreign", 404),
        (f"/rooms?course={course.id}&unit=foreign", 404),
        (f"/rooms/foreign?course={course.id}", 404),
        (f"/rooms?course={course.id}&unit=retired", 404),
    ]:
        assert (await room_client.get(route)).status_code == status
    for method, suffix, body in [
        ("PUT", "state", payload(state={})),
        ("POST", "complete", payload(action="complete", answer={"answer": 6})),
        ("POST", "review", payload()),
    ]:
        assert (
            await room_client.request(method, f"/rooms/foreign/{suffix}?course={course.id}", json=body)
        ).status_code == 404
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    assert (await room_client.get(f"/rooms?course={course.id}&unit=exercise")).status_code == 404
    outline = (await room_client.get(f"/courses/{course.id}/learning")).json()
    assert outline["units"][1]["selectable"] is False


async def test_explicit_course_continuation_follows_order_and_resumes_latest_draft(
    room_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    course = linked_course(monkeypatch)
    content.units[2].requires = ["a-concept-not-yet-introduced"]
    query = f"?course={course.id}"
    await room_client.put("/rooms/intro/state", json=payload(state={"older": True}))
    await room_client.put(f"/rooms/exercise/state{query}", json=payload(state={"newer": True}))
    assert (await room_client.get(f"/rooms?course={course.id}&continuous=true")).json()["next"]["unit"][
        "id"
    ] == "exercise"
    following = await room_client.get(f"/rooms?course={course.id}&after=exercise&continuous=true")
    assert following.json()["next"]["unit"]["id"] == "later"
    assert (await room_client.get(f"/rooms/later{query}")).status_code == 200
    assert (await room_client.get("/rooms/later")).status_code == 403
    # Merely opening a later unit does not alter the guided dashboard's knowledge.
    assert (await room_client.get("/rooms?continuous=true")).json()["next"]["unit"]["id"] == "intro"
    wrapped = await room_client.get(f"/rooms?course={course.id}&after=later&continuous=true")
    assert wrapped.json()["next"]["unit"]["id"] == "intro"
    await room_client.post(f"/rooms/later/complete{query}", json=payload(action="complete", answer={"answer": 6}))
    # An explicit next follows even a completed lesson as a fresh review, instead
    # of jumping back to an earlier unfinished lesson before the course boundary.
    following = await room_client.get(f"/rooms?course={course.id}&after=exercise&continuous=true")
    assert following.json()["next"]["unit"]["id"] == "later" and following.json()["next"]["review_available"]
    async with db_context():
        states = await rooms.read_states(USER.id)
        assert states["intro"].status == states["exercise"].status == "in_progress"
        assert "a-concept-not-yet-introduced" not in rooms.introduced_concepts(rooms.catalogue(), states)
