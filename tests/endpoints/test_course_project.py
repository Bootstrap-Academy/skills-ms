"""Course-wide project state (contract S1) on a disposable real SQL database, without network."""

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pytest import MonkeyPatch

from api import models
from api.app import request_validation_error_response
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints import course_project as project_endpoints
from api.endpoints.rooms import router as rooms_router
from api.schemas.course import Course
from api.schemas.course_project import SaveProject
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import course_project, rooms
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import settings
from api.utils.utc import utcnow

USER_A = "3f0c9a52-6a4e-4c1b-9d3e-5b8f2a7c1d01"
USER_B = "8a2d4e61-0b7f-4d9c-a1e3-7c6b5d4f3e02"
ADMIN = "5c1e7b93-2d4a-4f60-8e1b-9a3c7d5e2f03"
UNVERIFIED = "1b9d3f72-8c5e-4a1d-b7f6-2e4a9c8d1f04"
PROJECT = "/courses/llm-course/project"
CARD = {"bot": {"name": "Klingel", "tone": "freundlich"}, "cheatsheet": ["Öffnungszeiten 9–18 Uhr"]}


def body(revision: int = 0, state: Any = None, **values: Any) -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "state": CARD if state is None else state,
        **values,
    }


def course(course_id: str, price: int, path_id: str | None = "prompting") -> Course:
    return Course(
        id=course_id,
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
        learning_path_id=path_id,
    )


@pytest.fixture
def content(monkeypatch: MonkeyPatch) -> Catalogue:
    value = Catalogue.model_validate(
        {
            "paths": [
                {"id": "prompting", "title": {"de": "Pfad", "en": "Path"}, "units": ["intro"]},
                {"id": "other", "title": {"de": "Anderer", "en": "Other"}, "units": ["other-intro"]},
            ],
            "units": [
                {
                    "id": uid,
                    "path_id": path_id,
                    "title": {"de": uid, "en": uid},
                    "room": "guided-lesson",
                    "content": {"de": {"text": "Synthetic"}},
                    "teaches": [],
                    "practices": [],
                    "requires": [],
                    "retired": False,
                    "completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True},
                }
                for uid, path_id in (("intro", "prompting"), ("other-intro", "other"))
            ],
        }
    )
    courses = [
        course("llm-course", 0),
        course("second-course", 0, "other"),
        course("paid-course", 100),
        course("free-alias", 0),  # a free course on the same path as `paid-course`
        course("video-course", 0, None),
        course("missing-path", 0, "unknown-path"),
    ]
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(rooms, "load_catalogue", lambda: value)
    monkeypatch.setattr(rooms, "COURSES", {value.id: value for value in courses})
    monkeypatch.setattr(rooms, "has_premium", AsyncMock(return_value=False))
    return value


@pytest.fixture
async def client(content: Catalogue) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        users = {
            "Bearer a": User(id=USER_A, email_verified=True, admin=False),
            "Bearer b": User(id=USER_B, email_verified=True, admin=False),
            "Bearer admin": User(id=ADMIN, email_verified=True, admin=True),
            "Bearer unverified": User(id=UNVERIFIED, email_verified=False, admin=False),
        }
        token = request.headers.get("Authorization", "")
        if token not in users:
            raise HTTPException(401, "Synthetic missing authority")
        return users[token]

    app = FastAPI()
    app.exception_handler(RequestValidationError)(request_validation_error_response)
    app.dependency_overrides[user_auth.dependency] = identity
    app.include_router(project_endpoints.router, dependencies=[Depends(session)])
    app.include_router(rooms_router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://skills.synthetic",
        headers={"Authorization": "Bearer a"},
    ) as client:
        yield client


async def rows(model: Any, user_id: str = USER_A) -> list[Any]:
    async with db_context():
        return list(await db.all(filter_by(model, user_id=user_id)))


async def test_empty_project_is_revision_zero_and_a_read_writes_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get(PROJECT)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {"course_id": "llm-course", "revision": 0, "state": {}, "updated_at": None}
    for model in (models.CourseProject, models.CourseProjectRequest, models.PurchaseUser):
        assert await rows(model) == []


async def test_save_increments_revision_and_every_lesson_reads_it_back(client: httpx.AsyncClient) -> None:
    first = await client.put(PROJECT, json=body(0))
    assert first.status_code == 200 and first.headers["Cache-Control"] == "private, no-store"
    saved = first.json()
    assert set(saved) == {"course_id", "revision", "state", "updated_at"}
    assert (saved["course_id"], saved["revision"], saved["state"]) == ("llm-course", 1, CARD)
    assert datetime.fromisoformat(saved["updated_at"]).utcoffset() is not None
    assert (await client.get(PROJECT)).json() == saved
    grown = {**CARD, "tools": ["Terminkalender"]}
    second = await client.put(PROJECT, json=body(1, grown))
    assert second.status_code == 200 and second.json()["revision"] == 2 and second.json()["state"] == grown
    assert (await client.get(PROJECT)).json() == second.json()
    [row] = await rows(models.CourseProject)
    assert (row.course_id, row.revision, row.state) == ("llm-course", 2, grown)


@pytest.mark.parametrize("stale", [0, 2, 7])
async def test_revision_conflict_is_409_and_keeps_the_saved_state(client: httpx.AsyncClient, stale: int) -> None:
    saved = (await client.put(PROJECT, json=body(0))).json()
    response = await client.put(PROJECT, json=body(stale, {"overwritten": True}))
    assert response.status_code == 409
    assert (await client.get(PROJECT)).json() == saved
    assert len(await rows(models.CourseProjectRequest)) == 1


async def test_first_save_needs_revision_zero(client: httpx.AsyncClient) -> None:
    assert (await client.put(PROJECT, json=body(1))).status_code == 409
    assert await rows(models.CourseProject) == []


async def test_exact_replay_returns_the_stored_200_even_after_later_saves(client: httpx.AsyncClient) -> None:
    request = body(0)
    first = await client.put(PROJECT, json=request)
    assert first.status_code == 200
    replay = await client.put(PROJECT, json=request)
    assert replay.status_code == 200 and replay.json() == first.json()
    later = await client.put(PROJECT, json=body(1, {"later": True}))
    assert later.status_code == 200
    # Same keys in another order are the same body.
    reordered = {"state": request["state"], "expected_revision": 0, "request_id": request["request_id"]}
    assert (await client.put(PROJECT, json=reordered)).json() == first.json()
    assert (await client.get(PROJECT)).json() == later.json()
    [row] = await rows(models.CourseProject)
    assert row.revision == 2
    # Receipts never repeat the private state.
    assert len(await rows(models.CourseProjectRequest)) == 2
    assert "state" not in models.CourseProjectRequest.__table__.columns


@pytest.mark.parametrize("change", [{"state": {"bot": {"name": "Anders"}}}, {"expected_revision": 1}, {"state": {}}])
async def test_reused_request_id_with_another_body_is_409(client: httpx.AsyncClient, change: dict[str, Any]) -> None:
    request = body(0)
    saved = (await client.put(PROJECT, json=request)).json()
    response = await client.put(PROJECT, json={**request, **change})
    assert response.status_code == 409
    assert (await client.get(PROJECT)).json() == saved


async def test_reused_request_id_in_another_course_is_409(client: httpx.AsyncClient) -> None:
    request = body(0)
    assert (await client.put(PROJECT, json=request)).status_code == 200
    assert (await client.put("/courses/second-course/project", json=request)).status_code == 409
    assert (await client.get("/courses/second-course/project")).json()["revision"] == 0


async def test_projects_are_per_course(client: httpx.AsyncClient) -> None:
    assert (await client.put(PROJECT, json=body(0))).status_code == 200
    other = await client.put("/courses/second-course/project", json=body(0, {"second": True}))
    assert other.status_code == 200 and other.json()["course_id"] == "second-course" and other.json()["revision"] == 1
    assert (await client.get(PROJECT)).json()["state"] == CARD
    assert (await client.get("/courses/second-course/project")).json()["state"] == {"second": True}


async def test_size_limit_counts_compact_utf8_json(client: httpx.AsyncClient) -> None:
    # `{"s":""}` is 8 bytes, so this is exactly 64 KiB as `JSON.stringify` would produce it.
    exact = {"s": "x" * (65536 - 8)}
    assert (await client.put(PROJECT, json=body(0, exact))).status_code == 200
    assert (await client.put(PROJECT, json=body(1, {"s": "x" * (65536 - 7)}))).status_code == 413
    # Multi-byte characters count as UTF-8 bytes: 32 765 characters, 65 538 bytes.
    assert (await client.put(PROJECT, json=body(1, {"s": "ä" * 32765}))).status_code == 413
    [row] = await rows(models.CourseProject)
    assert row.revision == 1 and row.state == exact
    assert len(await rows(models.CourseProjectRequest)) == 1


@pytest.mark.parametrize("state", [[], ["a"], "text", 3, 1.5, True, None])
async def test_state_that_is_not_an_object_is_422(client: httpx.AsyncClient, state: Any) -> None:
    request = {"request_id": str(uuid4()), "expected_revision": 0, "state": state}
    assert (await client.put(PROJECT, json=request)).status_code == 422
    assert await rows(models.CourseProject) == []


@pytest.mark.parametrize(
    "request_body",
    [
        {"request_id": "not-a-uuid", "expected_revision": 0, "state": {}},
        {"expected_revision": 0, "state": {}},
        {"request_id": str(uuid4()), "state": {}},
        {"request_id": str(uuid4()), "expected_revision": 0},
        {"request_id": str(uuid4()), "expected_revision": -1, "state": {}},
        {"request_id": str(uuid4()), "expected_revision": True, "state": {}},
        {"request_id": str(uuid4()), "expected_revision": "0", "state": {}},
        {"request_id": str(uuid4()), "expected_revision": 0, "state": {}, "course_id": "paid-course"},
    ],
)
async def test_malformed_requests_are_422(client: httpx.AsyncClient, request_body: dict[str, Any]) -> None:
    assert (await client.put(PROJECT, json=request_body)).status_code == 422


async def test_non_finite_numbers_are_422(client: httpx.AsyncClient) -> None:
    raw = '{"request_id": "%s", "expected_revision": 0, "state": {"budget": NaN}}' % uuid4()
    response = await client.put(PROJECT, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


@pytest.mark.parametrize("state", [r'{"s": "\ud800"}', r'{"\udfff": 1}', r'{"deep": {"list": ["ok", "a\ud83d"]}}'])
async def test_lone_surrogate_is_422_not_500(client: httpx.AsyncClient, state: str) -> None:
    # Valid JSON escapes, but not UTF-8: no byte size, no storage. Other clients than ours can send them.
    raw = '{"request_id": "%s", "expected_revision": 0, "state": %s}' % (uuid4(), state)
    response = await client.put(PROJECT, content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert await rows(models.CourseProject) == [] and await rows(models.CourseProjectRequest) == []
    # A room state already refuses it the same way.
    room = '{"request_id": "%s", "expected_revision": 0, "state": %s}' % (uuid4(), state)
    response = await client.put("/rooms/intro/state", content=room, headers={"Content-Type": "application/json"})
    assert response.status_code == 422


async def test_surrogate_pairs_and_escaped_text_are_fine(client: httpx.AsyncClient) -> None:
    raw = r'{"request_id": "%s", "expected_revision": 0, "state": {"emoji": "\ud83d\ude00", "text": "\\ud800"}}'
    response = await client.put(PROJECT, content=raw % uuid4(), headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json()["state"] == {"emoji": "\U0001f600", "text": "\\ud800"}


async def test_state_is_private_per_user(client: httpx.AsyncClient) -> None:
    request = body(0)
    assert (await client.put(PROJECT, json=request)).status_code == 200
    as_b = {"Authorization": "Bearer b"}
    empty = await client.get(PROJECT, headers=as_b)
    assert empty.json() == {"course_id": "llm-course", "revision": 0, "state": {}, "updated_at": None}
    assert "Klingel" not in empty.text
    # A foreign user cannot overwrite or replay into A's project, even with A's request_id.
    assert (await client.put(PROJECT, json=body(1, {"b": 1}), headers=as_b)).status_code == 409
    replayed = await client.put(PROJECT, json={**request, "state": {"b": 2}}, headers=as_b)
    assert replayed.status_code == 200 and replayed.json()["state"] == {"b": 2} and "Klingel" not in replayed.text
    assert (await client.get(PROJECT)).json()["state"] == CARD
    assert [row.state for row in await rows(models.CourseProject, USER_B)] == [{"b": 2}]
    async with db_context():
        export = await export_user_data(USER_B)
    assert "Klingel" not in export.model_dump_json() and USER_A not in export.model_dump_json()


async def test_no_course_access_is_403_on_read_and_write(client: httpx.AsyncClient) -> None:
    paid = "/courses/paid-course/project"
    assert (await client.get(paid)).status_code == 403
    assert (await client.put(paid, json=body(0))).status_code == 403
    assert await rows(models.CourseProject) == []
    # A free course on the same path does not open the paid course's project.
    assert (await client.get("/courses/free-alias/project")).status_code == 200
    assert (await client.get(paid)).status_code == 403


@pytest.mark.parametrize("admission", ["purchased", "historical", "premium", "admin"])
async def test_course_access_is_the_lesson_check(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, admission: str
) -> None:
    paid = "/courses/paid-course/project"
    headers = {"Authorization": "Bearer admin"} if admission == "admin" else {}
    if admission == "premium":
        monkeypatch.setattr(rooms, "has_premium", AsyncMock(return_value=True))
    elif admission == "purchased":
        async with db_context():
            await db.add(models.CourseAccess(user_id=USER_A, course_id="paid-course"))
    elif admission == "historical":
        async with db_context():
            await models.LastWatch.update(USER_A, "paid-course")
    request = body(0)
    saved = await client.put(paid, json=request, headers=headers)
    assert saved.status_code == 200
    assert (await client.get(paid, headers=headers)).json() == saved.json()
    # Access is checked again before an exact retry is answered.
    monkeypatch.setattr(rooms, "has_premium", AsyncMock(return_value=False))
    monkeypatch.setattr(rooms, "get_owned_courses", AsyncMock(return_value=set()))
    if admission != "admin":
        assert (await client.put(paid, json=request)).status_code == 403
        assert (await client.get(paid)).status_code == 403


@pytest.mark.parametrize(
    "course_id,token,premium,owned",
    [
        (course_id, token, premium, owned)
        for course_id in ("llm-course", "paid-course", "free-alias", "second-course", "unknown")
        for token in ("a", "admin")
        for premium in (False, True)
        for owned in (False, True)
    ],
)
async def test_project_admission_equals_lesson_admission_in_that_course(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, course_id: str, token: str, premium: bool, owned: bool
) -> None:
    monkeypatch.setattr(rooms, "has_premium", AsyncMock(return_value=premium))
    monkeypatch.setattr(rooms, "get_owned_courses", AsyncMock(return_value={course_id} if owned else set()))
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    unit = "other-intro" if course_id == "second-course" else "intro"
    headers = {"Authorization": f"Bearer {token}"}
    lesson = await client.get(f"/rooms/{unit}?course={course_id}", headers=headers)
    project = await client.get(f"/courses/{course_id}/project", headers=headers)
    assert lesson.status_code in (200, 403, 404)
    assert project.status_code == lesson.status_code


@pytest.mark.parametrize("course_id", ["unknown", "video-course", "missing-path"])
async def test_course_without_lessons_is_404(client: httpx.AsyncClient, course_id: str) -> None:
    assert (await client.get(f"/courses/{course_id}/project")).status_code == 404
    assert (await client.put(f"/courses/{course_id}/project", json=body(0))).status_code == 404
    assert await rows(models.CourseProject) == []


async def test_gates_disabled_unauthenticated_and_unverified(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    assert (await client.get(PROJECT, headers={"Authorization": ""})).status_code == 401
    assert (await client.put(PROJECT, json=body(0), headers={"Authorization": ""})).status_code == 401
    unverified = {"Authorization": "Bearer unverified"}
    assert (await client.get(PROJECT, headers=unverified)).status_code == 403
    assert (await client.put(PROJECT, json=body(0), headers=unverified)).status_code == 403
    monkeypatch.setattr(settings, "rooms_enabled", False)
    assert (await client.get(PROJECT)).status_code == 404
    assert (await client.put(PROJECT, json=body(0))).status_code == 404
    for user_id in (USER_A, UNVERIFIED):
        assert await rows(models.CourseProject, user_id) == []


async def test_export_and_erasure_include_the_project(client: httpx.AsyncClient, monkeypatch: MonkeyPatch) -> None:
    request = body(0)
    saved = (await client.put(PROJECT, json=request)).json()
    assert (await client.put(PROJECT, json=body(0, {"b": 1}), headers={"Authorization": "Bearer b"})).status_code == 200
    async with db_context():
        export = await export_user_data(USER_A)
    [project] = export.course_projects
    assert (project["user_id"], project["course_id"], project["revision"], project["state"]) == (
        USER_A,
        "llm-course",
        1,
        CARD,
    )
    [receipt] = export.course_project_requests
    assert receipt["request_id"] == request["request_id"] and receipt["revision"] == saved["revision"]
    assert all(row["user_id"] == USER_A for row in export.course_projects)
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await delete_user_data(USER_A)
    # A late retry cannot recreate the erased project.
    assert (await client.put(PROJECT, json=request)).status_code == 401
    assert (await client.get(PROJECT)).status_code == 401
    async with db_context():
        export = await export_user_data(USER_A)
    assert export.course_projects == export.course_project_requests == []
    assert [row.state for row in await rows(models.CourseProject, USER_B)] == [{"b": 1}]


async def test_receipts_are_bounded_and_old_retries_still_conflict(client: httpx.AsyncClient) -> None:
    requests, results = [], []
    for revision in range(course_project.KEPT_RECEIPTS + 2):
        requests.append(body(revision, {"step": revision}))
        response = await client.put(PROJECT, json=requests[-1])
        assert response.status_code == 200
        results.append(response.json())
    kept = course_project.KEPT_RECEIPTS
    assert (await client.put(PROJECT, json=requests[-kept])).json() == results[-kept]
    assert (await client.put(PROJECT, json=requests[-kept - 1])).status_code == 409
    receipts = await rows(models.CourseProjectRequest)
    assert {receipt.revision for receipt in receipts} == set(range(3, kept + 3))


async def test_transaction_rollback_removes_state_and_receipt(content: Catalogue) -> None:
    user = User(id=USER_A, email_verified=True, admin=False)
    data = SaveProject.model_validate(body(0))
    with pytest.raises(RuntimeError):
        async with db_context():
            await course_project.save_project("llm-course", user, data)
            raise RuntimeError("Synthetic lost transaction")
    assert await rows(models.CourseProject) == []
    assert await rows(models.CourseProjectRequest) == []
    async with db_context():
        assert (await course_project.save_project("llm-course", user, data)).revision == 1


@pytest.mark.parametrize("initial_revision", [0, 1])
async def test_concurrent_stale_writes_keep_one_revision(content: Catalogue, initial_revision: int) -> None:
    user = User(id=USER_A, email_verified=True, admin=False)
    async with db_context():
        await db.add(models.PurchaseUser(user_id=USER_A, deleted=False))
        if initial_revision:
            await db.add(
                models.CourseProject(
                    user_id=USER_A, course_id="llm-course", revision=initial_revision, state={}, updated_at=utcnow()
                )
            )

    async def write(value: int) -> int:
        try:
            async with db_context():
                data = SaveProject.model_validate(body(initial_revision, {"value": value}))
                return (await course_project.save_project("llm-course", user, data)).revision
        except HTTPException as exc:
            return exc.status_code

    assert sorted(await asyncio.gather(write(1), write(2))) == [initial_revision + 1, 409]
    assert len(await rows(models.CourseProjectRequest)) == 1
