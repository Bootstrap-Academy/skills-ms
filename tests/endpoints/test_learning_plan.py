"""The learning plan ("Dein Lernweg"), its suggestions and a learner's view of a track, on a disposable SQL database."""

import logging
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from pytest import MonkeyPatch
from pytest_mock import MockerFixture
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from api import models
from api.app import request_validation_error_response
from api.auth import public_auth
from api.database import db, db_context, filter_by
from api.endpoints import learning_plan as plan_endpoints
from api.endpoints import tracks as track_endpoints
from api.exceptions.api_exception import CodedAPIException
from api.models.root_skill import RootSkillDependency
from api.schemas.course import Course
from api.schemas.learning_plan import PLAN_STATE_LIMIT, PlanState
from api.schemas.rooms import Catalogue
from api.schemas.tracks import TrackDefinition
from api.schemas.user import User
from api.services import learning_plan, rooms
from api.services.courses import COURSES
from api.services.tracks import TRACKS
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import settings
from api.utils.debug import check_responses
from api.utils.utc import utcnow

USER_A = "6d1f3a85-2b4c-4e7d-9a10-3c5b7e9f1a01"
USER_B = "9e2a4b76-3c5d-4f8e-8b21-4d6c8f0a2b02"
UNVERIFIED = "2c8e5d63-7a4b-4c1e-a9f2-5e7a1c3d4f03"
USERS = {
    "Bearer a": User(id=USER_A, email_verified=True, admin=False),
    "Bearer b": User(id=USER_B, email_verified=True, admin=False),
    "Bearer unverified": User(id=UNVERIFIED, email_verified=False, admin=False),
}
STATE: dict[str, Any] = {
    "character": "einsteiger",
    "preference": "breit",
    "steps": [
        {"type": "course", "course": "synthetic-video", "track": "synthetic-track", "stage": "fundamentals"},
        {"type": "course", "course": "synthetic-path"},
        {"type": "external", "track": "synthetic-track", "stage": "practitioner", "id": "workshop", "done": True},
        {"type": "course", "course": "docker"},
    ],
}


def body(revision: int = 0, state: Any = None, **values: Any) -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "state": STATE if state is None else state,
        **values,
    }


def course(course_id: str, **values: Any) -> Course:
    return Course.model_validate(
        {
            "id": course_id,
            "title": course_id.replace("-", " ").capitalize(),
            "authors": [],
            "price": 0,
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            **values,
        }
    )


def lectures(*ids: str) -> list[dict[str, Any]]:
    return [{"id": lid, "title": lid, "video_id": "synthetic", "duration": 1} for lid in ids]


@pytest.fixture(autouse=True)
def content(monkeypatch: MonkeyPatch) -> Catalogue:
    catalogue = Catalogue.model_validate(
        {
            "paths": [{"id": "synthetic-path", "title": {"de": "Pfad", "en": "Path"}, "units": ["u1", "u2", "u3"]}],
            "units": [
                {
                    "id": uid,
                    "path_id": "synthetic-path",
                    "title": {"de": uid, "en": uid},
                    "room": "guided-lesson",
                    "content": {"de": {"text": "Synthetic"}},
                    "teaches": [],
                    "practices": [],
                    "requires": [],
                    "retired": uid == "u3",
                    "completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True},
                }
                for uid in ("u1", "u2", "u3")
            ],
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(rooms, "load_catalogue", lambda: catalogue)
    synthetic = [
        course(
            "synthetic-video",
            sections=[
                {"id": "one", "title": "One", "lectures": lectures("l1", "l2")},
                {"id": "two", "title": "Two", "lectures": lectures("l3")},
            ],
            translations={"en": {"title": "Synthetic video in English"}},
        ),
        course("synthetic-path", learning_path_id="synthetic-path"),
        course(
            "synthetic-composed",
            learning_path_id="synthetic-path",
            curriculum={
                "lessons": [
                    {"id": "first", "title": {"de": "Erste", "en": "First"}, "activities": [room("u1")]},
                    {"id": "second", "title": {"de": "Zweite", "en": "Second"}, "activities": [room("u2")]},
                ]
            },
        ),
    ]
    for item in synthetic:
        monkeypatch.setitem(COURSES, item.id, item)
    track = TrackDefinition.model_validate(
        {
            "id": "synthetic-track",
            "title": {"de": "Synthetischer Pfad", "en": "Synthetic track"},
            "level": "einsteiger",
            "stages": [
                {
                    "id": "fundamentals",
                    "title": "Grundlagen",
                    "kind": "fundamentals",
                    "items": [{"course": "synthetic-video"}, {"course": "synthetic-path"}],
                },
                {
                    "id": "practitioner",
                    "title": "Praxis",
                    "kind": "practitioner",
                    "items": [
                        {
                            "external": {
                                "id": "workshop",
                                "title": {"de": "Workshop Practitioner", "en": "Practitioner workshop"},
                                "url": "https://example.invalid/workshop",
                            }
                        },
                        {"external": {"id": "exam", "title": "Prüfung"}},
                    ],
                },
            ],
        }
    )
    monkeypatch.setitem(TRACKS, track.id, track)
    return catalogue


def room(unit_id: str) -> dict[str, Any]:
    return {"id": unit_id, "source": {"kind": "room", "unit_id": unit_id}}


async def coded_errors(request: Request, exc: Exception) -> Response:
    # As `rollback_on_exception` (api/app.py) answers, without the session of the deployed middleware.
    if isinstance(exc, CodedAPIException):
        return exc.response()
    return await http_exception_handler(request, cast(StarletteHTTPException, exc))


def make_app() -> FastAPI:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User | None:
        return USERS.get(request.headers.get("Authorization", ""))

    app = FastAPI()
    app.exception_handler(RequestValidationError)(request_validation_error_response)
    app.exception_handler(StarletteHTTPException)(coded_errors)
    app.dependency_overrides[public_auth.dependency] = identity
    app.include_router(plan_endpoints.router, dependencies=[Depends(session)])
    app.include_router(track_endpoints.router, dependencies=[Depends(session)])
    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()),
        base_url="http://skills.synthetic",
        headers={"Authorization": "Bearer a"},
    ) as client:
        yield client


async def rows(model: Any, user_id: str = USER_A) -> list[Any]:
    async with db_context():
        return list(await db.all(filter_by(model, user_id=user_id)))


async def watch(user_id: str, course_id: str, *lecture_ids: str) -> None:
    async with db_context():
        for lecture_id in lecture_ids:
            await models.LectureProgress.set_completed(user_id, course_id, lecture_id)


async def room_state(user_id: str, unit_id: str, status: str) -> None:
    async with db_context():
        await db.add(
            models.RoomState(
                user_id=user_id,
                unit_id=unit_id,
                revision=1,
                state={},
                status=status,
                result={"kind": "introduced"} if status == "completed" else None,
                updated_at=utcnow(),
            )
        )


def docker_lectures() -> int:
    return sum(len(section.lectures) for section in COURSES["docker"].sections)


async def test_without_a_plan_it_is_revision_zero_and_a_read_writes_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get("/plan")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {"revision": 0, "character": None, "preference": None, "steps": [], "next": None}
    for model in (models.LearningPlan, models.PurchaseUser):
        assert await rows(model) == []


async def test_a_saved_plan_comes_back_with_the_progress_of_each_step(client: httpx.AsyncClient) -> None:
    await watch(USER_A, "synthetic-video", "l2")
    await room_state(USER_A, "u1", "completed")
    saved = await client.put("/plan", json=body())
    assert saved.status_code == 200 and saved.headers["Cache-Control"] == "private, no-store"
    assert saved.json() == {
        "revision": 1,
        "character": "einsteiger",
        "preference": "breit",
        "steps": [
            {
                "type": "course",
                "course": "synthetic-video",
                "title": "Synthetic video",
                "track": "synthetic-track",
                "stage": "fundamentals",
                "status": "in_progress",
                "done_units": 1,
                "total_units": 3,
            },
            {
                "type": "course",
                "course": "synthetic-path",
                "title": "Synthetic path",
                "track": None,
                "stage": None,
                "status": "in_progress",
                "done_units": 1,
                "total_units": 2,
            },
            {
                "type": "external",
                "id": "workshop",
                "title": "Workshop Practitioner",
                "url": "https://example.invalid/workshop",
                "track": "synthetic-track",
                "stage": "practitioner",
                "status": "done",
                "self_confirmed": True,
            },
            {
                "type": "course",
                "course": "docker",
                "title": "Docker",
                "track": None,
                "stage": None,
                "status": "open",
                "done_units": 0,
                "total_units": docker_lectures(),
            },
        ],
        "next": {"type": "course", "course": "synthetic-video"},
    }
    assert (await client.get("/plan")).json() == saved.json()
    [row] = await rows(models.LearningPlan)
    # Stored as sent, without the unset fields; nothing derived is stored.
    assert row.revision == 1 and row.state == {
        "character": "einsteiger",
        "preference": "breit",
        "steps": [
            {"type": "course", "course": "synthetic-video", "track": "synthetic-track", "stage": "fundamentals"},
            {"type": "course", "course": "synthetic-path"},
            {"type": "external", "track": "synthetic-track", "stage": "practitioner", "id": "workshop", "done": True},
            {"type": "course", "course": "docker"},
        ],
    }
    english = await client.get("/plan", headers={"Accept-Language": "en"})
    assert [step["title"] for step in english.json()["steps"]][:3] == [
        "Synthetic video in English",
        "Synthetic path",
        "Practitioner workshop",
    ]


async def test_courses_are_done_by_their_completion_rules(client: httpx.AsyncClient) -> None:
    state = {
        "steps": [
            {"type": "course", "course": "synthetic-video"},
            {"type": "course", "course": "synthetic-path"},
            {"type": "course", "course": "synthetic-composed"},
            {"type": "external", "track": "synthetic-track", "stage": "practitioner", "id": "exam"},
        ]
    }
    assert (await client.put("/plan", json=body(0, state))).status_code == 200
    await watch(USER_A, "synthetic-video", "l1", "l2", "l3")
    await room_state(USER_A, "u1", "completed")
    await room_state(USER_A, "u2", "skipped")
    plan = (await client.get("/plan")).json()
    # The retired unit u3 does not count; a skipped unit is finished, as for the course summary.
    assert [(step["status"], step.get("done_units"), step.get("total_units")) for step in plan["steps"]] == [
        ("done", 3, 3),
        ("done", 2, 2),
        ("done", 2, 2),
        ("open", None, None),
    ]
    assert plan["steps"][3]["self_confirmed"] is False
    assert plan["next"] == {"type": "external", "id": "exam", "track": "synthetic-track", "stage": "practitioner"}


async def test_without_the_rooms_a_path_course_is_open_and_not_counted(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await room_state(USER_A, "u1", "completed")
    state = {"steps": [{"type": "course", "course": "synthetic-path"}, {"type": "course", "course": "synthetic-video"}]}
    assert (await client.put("/plan", json=body(0, state))).status_code == 200
    monkeypatch.setattr(settings, "rooms_enabled", False)
    steps = (await client.get("/plan")).json()["steps"]
    assert [(step["status"], step["done_units"], step["total_units"]) for step in steps] == [
        ("open", None, None),
        ("open", 0, 3),
    ]


@pytest.mark.parametrize("stale", [0, 2, 9])
async def test_a_stale_revision_is_409_with_the_current_plan(client: httpx.AsyncClient, stale: int) -> None:
    saved = (await client.put("/plan", json=body(0))).json()
    response = await client.put("/plan", json=body(stale, {"steps": []}))
    assert response.status_code == 409 and response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {
        "detail": "Your learning plan has changed in another session",
        "code": "plan_conflict",
        "plan": saved,
    }
    assert (await client.get("/plan")).json() == saved


async def test_the_first_save_needs_revision_zero(client: httpx.AsyncClient) -> None:
    response = await client.put("/plan", json=body(1))
    assert response.status_code == 409 and response.json()["plan"]["revision"] == 0
    assert await rows(models.LearningPlan) == []


async def test_an_exact_retry_of_the_latest_save_gets_the_same_answer(client: httpx.AsyncClient) -> None:
    request = body(0)
    first = await client.put("/plan", json=request)
    assert first.status_code == 200
    assert (await client.put("/plan", json=request)).json() == first.json()
    # Keys in another order and unset fields sent as null are the same request.
    nulls = [STATE["steps"][0], {**STATE["steps"][1], "track": None, "stage": None}, *STATE["steps"][2:]]
    same = {"state": {**STATE, "steps": nulls}, "expected_revision": 0, "request_id": request["request_id"]}
    assert (await client.put("/plan", json=same)).json() == first.json()
    reordered = {
        "expected_revision": 0,
        "state": dict(reversed(list(STATE.items()))),
        "request_id": request["request_id"],
    }
    assert (await client.put("/plan", json=reordered)).json() == first.json()
    # Another order of the steps is another plan.
    swapped = {**request, "state": {**STATE, "steps": STATE["steps"][::-1]}}
    assert (await client.put("/plan", json=swapped)).status_code == 409
    [row] = await rows(models.LearningPlan)
    assert row.revision == 1


@pytest.mark.parametrize("change", [{"state": {"steps": []}}, {"expected_revision": 1}])
async def test_a_reused_request_id_with_another_request_is_409(
    client: httpx.AsyncClient, change: dict[str, Any]
) -> None:
    request = body(0)
    saved = (await client.put("/plan", json=request)).json()
    response = await client.put("/plan", json={**request, **change})
    assert response.status_code == 409 and response.json()["code"] == "request_reused"
    assert response.json()["plan"] == saved == (await client.get("/plan")).json()


async def test_a_retry_of_an_older_save_is_a_conflict(client: httpx.AsyncClient) -> None:
    older = body(0)
    assert (await client.put("/plan", json=older)).status_code == 200
    later = await client.put("/plan", json=body(1, {"character": "profi"}))
    assert later.status_code == 200
    response = await client.put("/plan", json=older)
    assert response.status_code == 409 and response.json()["plan"] == later.json()


async def test_the_size_limit_is_16_kib_of_the_stored_state(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    assert PLAN_STATE_LIMIT == 16384
    stored = learning_plan.normalized(PlanState.model_validate(STATE))
    monkeypatch.setattr(learning_plan, "PLAN_STATE_LIMIT", learning_plan.state_size(stored))
    # Nulls are not counted: they are not stored.
    nulls = {
        **STATE,
        "steps": [
            {**step, "track": None} if step["type"] == "course" and "track" not in step else step
            for step in STATE["steps"]
        ],
    }
    assert (await client.put("/plan", json=body(0, nulls))).status_code == 200
    monkeypatch.setattr(learning_plan, "PLAN_STATE_LIMIT", learning_plan.state_size(stored) - 1)
    response = await client.put("/plan", json=body(1))
    assert response.status_code == 413 and response.json() == {
        "detail": "The learning plan is larger than 16 KiB",
        "code": "plan_too_large",
    }
    [row] = await rows(models.LearningPlan)
    assert row.revision == 1


async def test_a_plan_over_16_kib_is_413_before_its_steps_are_checked(client: httpx.AsyncClient) -> None:
    steps = [{"type": "course", "course": f"unknown-{index:04d}"} for index in range(500)]
    response = await client.put("/plan", json=body(0, {"steps": steps}))
    assert response.status_code == 413
    assert await rows(models.LearningPlan) == []


@pytest.mark.parametrize(
    "steps,code,index",
    [
        ([{"type": "course", "course": "no-such-course"}], "unknown_course", 0),
        (
            [{"type": "course", "course": "linux", "track": "no-such-track", "stage": "fundamentals"}],
            "unknown_track",
            0,
        ),
        ([{"type": "course", "course": "linux", "stage": "practitioner"}], "unknown_track", 0),
        ([{"type": "course", "course": "linux", "track": "einstieg-it"}], "unknown_stage", 0),
        ([{"type": "course", "course": "linux", "track": "einstieg-it", "stage": "mastery"}], "unknown_stage", 0),
        ([{"type": "course", "course": "docker", "track": "einstieg-it", "stage": "practitioner"}], "unknown_step", 0),
        ([{"type": "external", "track": "cloud-engineer", "stage": "practitioner", "id": "nope"}], "unknown_step", 0),
        (
            [{"type": "external", "track": "cloud-engineer", "stage": "mastery", "id": "workshop-practitioner"}],
            "unknown_step",
            0,
        ),
        ([{"type": "external", "track": "einstieg-it", "stage": "nope", "id": "x"}], "unknown_stage", 0),
        ([{"type": "external", "track": "nope", "stage": "fundamentals", "id": "x"}], "unknown_track", 0),
        (
            [
                {"type": "course", "course": "linux"},
                {"type": "course", "course": "linux", "track": "einstieg-it", "stage": "practitioner"},
            ],
            "duplicate_step",
            1,
        ),
        (
            [
                {"type": "course", "course": "docker"},
                {"type": "external", "track": "cloud-engineer", "stage": "mastery", "id": "finops"},
                {"type": "external", "track": "cloud-engineer", "stage": "mastery", "id": "finops", "done": True},
            ],
            "duplicate_step",
            2,
        ),
    ],
)
async def test_unknown_or_repeated_steps_are_422(
    client: httpx.AsyncClient, steps: list[dict[str, Any]], code: str, index: int
) -> None:
    response = await client.put("/plan", json=body(0, {"steps": steps}))
    assert response.status_code == 422
    assert response.json()["code"] == code and response.json()["step"] == index
    assert set(response.json()) == {"detail", "code", "step"}
    assert await rows(models.LearningPlan) == []


@pytest.mark.parametrize(
    "state",
    [
        {"character": "experte"},
        {"preference": "mittel"},
        {"steps": [{"type": "video", "course": "linux"}]},
        {"steps": [{"type": "course"}]},
        {"steps": [{"type": "course", "course": "Linux"}]},
        {"steps": [{"type": "course", "course": "linux", "done": True}]},
        {"steps": [{"type": "external", "stage": "practitioner", "id": "workshop-practitioner"}]},
        {"steps": [{"type": "external", "track": "cloud-engineer", "stage": "practitioner", "id": "x", "done": "yes"}]},
        {"steps": {"type": "course", "course": "linux"}},
        {"unknown": True},
        [],
        None,
    ],
)
async def test_malformed_plans_are_422(client: httpx.AsyncClient, state: Any) -> None:
    request = {"request_id": str(uuid4()), "expected_revision": 0, "state": state}
    assert (await client.put("/plan", json=request)).status_code == 422
    assert await rows(models.LearningPlan) == []


@pytest.mark.parametrize(
    "request_body",
    [
        {"request_id": "not-a-uuid", "expected_revision": 0, "state": {}},
        {"expected_revision": 0, "state": {}},
        {"request_id": str(uuid4()), "state": {}},
        {"request_id": str(uuid4()), "expected_revision": "0", "state": {}},
        {"request_id": str(uuid4()), "expected_revision": -1, "state": {}},
        {"request_id": str(uuid4()), "expected_revision": 0},
    ],
)
async def test_malformed_requests_are_422(client: httpx.AsyncClient, request_body: dict[str, Any]) -> None:
    assert (await client.put("/plan", json=request_body)).status_code == 422


async def test_the_plan_is_private_per_learner(client: httpx.AsyncClient) -> None:
    request = body(0)
    assert (await client.put("/plan", json=request)).status_code == 200
    as_b = {"Authorization": "Bearer b"}
    assert (await client.get("/plan", headers=as_b)).json()["steps"] == []
    # B cannot overwrite A's plan; A's request id is just a new request for B.
    assert (await client.put("/plan", json=body(1, {"steps": []}), headers=as_b)).status_code == 409
    own = await client.put("/plan", json={**request, "state": {"character": "profi"}}, headers=as_b)
    assert own.status_code == 200 and own.json()["character"] == "profi" and own.json()["steps"] == []
    assert (await client.get("/plan")).json()["character"] == "einsteiger"


@pytest.mark.parametrize("method,path", [("GET", "/plan"), ("PUT", "/plan"), ("GET", "/plan/suggestions")])
@pytest.mark.parametrize("token,status", [(None, 401), ("Bearer unknown", 401), ("Bearer unverified", 403)])
async def test_the_plan_needs_a_verified_learner(
    client: httpx.AsyncClient, method: str, path: str, token: str | None, status: int
) -> None:
    headers = {"Authorization": token} if token else {}
    request = client.build_request(method, path, json=body(0) if method == "PUT" else None, headers=headers)
    if token is None:
        del request.headers["Authorization"]
    assert (await client.send(request)).status_code == status
    assert await rows(models.LearningPlan, UNVERIFIED) == []


async def test_an_erased_account_is_401_everywhere(client: httpx.AsyncClient, mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    assert (await client.put("/plan", json=body(0))).status_code == 200
    async with db_context():
        await delete_user_data(USER_A)
    for method, path in [
        ("GET", "/plan"),
        ("PUT", "/plan"),
        ("GET", "/plan/suggestions"),
        ("GET", "/tracks/synthetic-track"),
    ]:
        response = await client.request(method, path, json=body(1) if method == "PUT" else None)
        assert response.status_code == 401, (method, path)
    assert await rows(models.LearningPlan) == []


async def test_export_and_erasure_cover_the_plan(client: httpx.AsyncClient, mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    saved = await client.put("/plan", json=body(0))
    assert (
        await client.put("/plan", json=body(0, {"character": "profi"}), headers={"Authorization": "Bearer b"})
    ).status_code == 200
    async with db_context():
        export = await export_user_data(USER_A)
    [row] = export.learning_plan
    assert set(row) == {"user_id", "revision", "state", "request_id", "request_fingerprint", "updated_at"}
    assert (row["user_id"], row["revision"], row["state"]["steps"][3]) == (
        USER_A,
        1,
        {"type": "course", "course": "docker"},
    )
    encoded = export.model_dump(mode="json")["learning_plan"][0]
    assert isinstance(encoded["updated_at"], str) and USER_B not in export.model_dump_json()
    assert saved.json()["revision"] == row["revision"]
    async with db_context():
        await delete_user_data(USER_A)
    assert await rows(models.LearningPlan) == []
    assert [plan.state for plan in await rows(models.LearningPlan, USER_B)] == [{"character": "profi", "steps": []}]


async def test_a_step_whose_content_is_gone_is_left_out(client: httpx.AsyncClient, monkeypatch: MonkeyPatch) -> None:
    assert (await client.put("/plan", json=body(0))).status_code == 200
    monkeypatch.delitem(TRACKS, "synthetic-track")
    monkeypatch.delitem(COURSES, "synthetic-path")
    plan = (await client.get("/plan")).json()
    # The course stays without its track; the external step and the removed course are left out.
    assert [(step["course"], step["track"], step["stage"]) for step in plan["steps"]] == [
        ("synthetic-video", None, None),
        ("docker", None, None),
    ]
    [row] = await rows(models.LearningPlan)
    assert len(row.state["steps"]) == 4


async def test_an_unreadable_stored_plan_is_503_without_its_contents(client: httpx.AsyncClient) -> None:
    async with db_context():
        await db.add(
            models.LearningPlan(
                user_id=USER_A,
                revision=1,
                state={"steps": [{"type": "PRIVATE-PLAN-CONTENT"}]},
                request_id="request",
                request_fingerprint="f" * 64,
                updated_at=utcnow(),
            )
        )
    response = await client.get("/plan")
    assert response.status_code == 503 and "PRIVATE-PLAN-CONTENT" not in response.text
    assert (await client.get("/tracks/synthetic-track")).status_code == 503


async def test_a_learner_sees_the_status_of_every_item_of_a_track(client: httpx.AsyncClient) -> None:
    await watch(USER_A, "synthetic-video", "l1", "l2", "l3")
    await room_state(USER_A, "u2", "in_progress")
    assert (await client.put("/plan", json=body(0))).status_code == 200
    response = await client.get("/tracks/synthetic-track")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    items = [item for stage in response.json()["stages"] for item in stage["items"]]
    assert [(item.get("course") or item["id"], item["status"]) for item in items] == [
        ("synthetic-video", "done"),
        ("synthetic-path", "in_progress"),
        ("workshop", "done"),
        ("exam", "open"),
    ]
    guest = await client.get("/tracks/synthetic-track", headers={"Authorization": ""})
    assert guest.headers["Cache-Control"] == "public, max-age=300" and "status" not in guest.text
    assert (await client.get("/tracks/synthetic-track", headers={"Authorization": "Bearer b"})).json()["stages"][1][
        "items"
    ][0]["status"] == "open"


async def add_tree() -> None:
    """A small tree on real courses: start (it-foundations, medienkompetenz) <- os (linux <- bash)."""
    async with db_context():
        for root in ("start", "os"):
            await db.add(models.RootSkill(id=root, name=root, row=0, column=0, sub_tree_rows=5, sub_tree_columns=5))
        await db.add(RootSkillDependency(dependent_id="os", dependency_id="start"))
        for skill, root in [
            ("it_grundlagen", "start"),
            ("medien", "start"),
            ("linux_grundlagen", "os"),
            ("bash_skill", "os"),
        ]:
            await db.add(models.SubSkill(id=skill, parent_id=root, name=skill, row=0, column=0))
        await db.add(models.SubSkillDependency(dependent_id="bash_skill", dependency_id="linux_grundlagen"))
        for skill, course_id in [
            ("it_grundlagen", "it-foundations"),
            ("medien", "medienkompetenz"),
            ("linux_grundlagen", "linux"),
            ("bash_skill", "bash"),
            ("bash_skill", "missing-course"),
        ]:
            await db.add(models.SkillCourse(skill_id=skill, course_id=course_id))


async def test_suggestions_follow_progress_and_plan_and_are_not_stored(client: httpx.AsyncClient) -> None:
    await add_tree()
    fresh = await client.get("/plan/suggestions")
    assert fresh.status_code == 200 and fresh.headers["Cache-Control"] == "private, no-store"
    assert fresh.json() == [
        {
            "type": "course",
            "course": "it-foundations",
            "title": "IT-Grundlagen: Computer wirklich verstehen",
            "reason": "new_area",
        },
        {"type": "course", "course": "medienkompetenz", "title": "Medienkompetenz", "reason": "new_area"},
        {"type": "course", "course": "linux", "title": "Erste Schritte mit Linux", "reason": "level_fit"},
        {"type": "course", "course": "bash", "title": "Bash", "reason": "level_fit"},
    ]
    every = [lecture.id for section in COURSES["medienkompetenz"].sections for lecture in section.lectures]
    await watch(USER_A, "medienkompetenz", *every)
    assert (
        await client.put("/plan", json=body(0, {"steps": [{"type": "course", "course": "it-foundations"}]}))
    ).status_code == 200
    found = (await client.get("/plan/suggestions", headers={"Accept-Language": "en"})).json()
    assert found == [
        {
            "type": "course",
            "course": "linux",
            "title": "Erste Schritte mit Linux",
            "reason": "next_after",
            "reason_course": "it-foundations",
        },
        {"type": "course", "course": "bash", "title": "Bash", "reason": "level_fit"},
    ]
    async with db_context():
        await db.add(models.XP(id="xp", user_id=USER_A, skill_id="linux_grundlagen", xp=20, last_update=utcnow()))
    deep = await client.put(
        "/plan", json=body(1, {"preference": "tief", "steps": [{"type": "course", "course": "it-foundations"}]})
    )
    assert deep.status_code == 200
    assert [(item["course"], item["reason"]) for item in (await client.get("/plan/suggestions")).json()] == [
        ("linux", "next_after"),
        # Its prerequisite is covered by the XP in Linux now, and its root follows the planned course's root.
        ("bash", "next_after"),
    ]
    [row] = await rows(models.LearningPlan)
    assert row.revision == 2 and "suggestion" not in str(row.state)


async def test_every_answer_passes_the_debug_check(mocker: MockerFixture) -> None:
    logger = mocker.patch("api.utils.debug.logger")
    checked = BaseHTTPMiddleware(make_app(), dispatch=cast(Any, check_responses))
    await add_tree()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=checked),
        base_url="http://skills.synthetic",
        headers={"Authorization": "Bearer a"},
    ) as checked_client:
        answers = [
            await checked_client.get("/plan"),
            await checked_client.put("/plan", json=body(0)),
            await checked_client.put("/plan", json=body(0)),
            await checked_client.put(
                "/plan", json=body(1, {"steps": [{"type": "course", "course": f"u{n}"} for n in range(800)]})
            ),
            await checked_client.put(
                "/plan", json=body(1, {"steps": [{"type": "course", "course": "no-such-course"}]})
            ),
            await checked_client.get("/plan/suggestions"),
            await checked_client.get("/tracks"),
            await checked_client.get("/tracks/synthetic-track"),
            await checked_client.get("/tracks/synthetic-track", headers={"Authorization": ""}),
            await checked_client.get("/tracks/unknown"),
            await checked_client.get("/plan", headers={"Authorization": ""}),
            await checked_client.get("/plan", headers={"Authorization": "Bearer unverified"}),
        ]
    assert [answer.status_code for answer in answers] == [200, 200, 409, 413, 422, 200, 200, 200, 200, 404, 401, 403]
    logger.error.assert_not_called()


async def test_plan_contents_never_reach_the_logs(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    marker = {"steps": [{"type": "course", "course": "kryptographie"}], "character": "fortgeschritten"}
    with caplog.at_level(logging.DEBUG):
        assert (await client.put("/plan", json=body(0, marker))).status_code == 200
        assert (await client.put("/plan", json=body(0, marker))).status_code == 409
        assert (await client.get("/plan")).status_code == 200
        unknown = {"steps": [{"type": "course", "course": "private-marker-course"}]}
        refused = await client.put("/plan", json=body(1, unknown))
        assert refused.status_code == 422 and "private-marker-course" not in refused.text
        assert (await client.get("/plan/suggestions")).status_code == 200
    # The SQLite driver of these tests logs its statements with their parameters at DEBUG; the service runs on
    # asyncpg or aiomysql, which do not.
    logged = "\n".join(
        f"{record.name} {record.getMessage()}" for record in caplog.records if not record.name.startswith("aiosqlite")
    )
    assert "kryptographie" not in logged and "private-marker-course" not in logged and "fortgeschritten" not in logged
