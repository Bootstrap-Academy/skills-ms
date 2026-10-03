"""Routed admission checks with a real disposable SQL database."""

from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pytest import MonkeyPatch

from api import models
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints import course as course_endpoints
from api.endpoints import curriculum as curriculum_endpoints
from api.endpoints.daily_limit import router as daily_router
from api.endpoints.rooms import router as room_router
from api.redis import redis
from api.schemas.course import Course, Section
from api.schemas.daily_limit import (
    ChallengeAdmission,
    LearningHistory,
    LearningPolicy,
    LectureBinding,
    LimitConfiguration,
)
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import courses, daily_limit, rooms, shop
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import settings
from api.utils.utc import utcnow


USER = User(id="daily-user", admin=False, email_verified=True)
REAL_POLICY = daily_limit.policy


@pytest.fixture
def catalog(monkeypatch: MonkeyPatch) -> Course:
    units = [
        {
            "id": f"unit-{i}",
            "path_id": "daily-path",
            "title": {"de": "Beispiel", "en": "Example"},
            "room": "loop-explorer",
            "content": {},
            "teaches": [],
            "practices": [],
            "requires": [],
            "retired": False,
            "completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True},
        }
        for i in range(6)
    ]
    content = Catalogue.parse_obj(
        {
            "paths": [{"id": "daily-path", "title": {"de": "Pfad", "en": "Path"}, "units": [u["id"] for u in units]}],
            "units": units,
        }
    )
    course = Course.parse_obj(
        {
            "id": "daily-course",
            "title": "Beispiel",
            "description": None,
            "category": None,
            "language": "de",
            "image": None,
            "authors": [],
            "price": 0,
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            "learning_path_id": "daily-path",
            "sections": [],
            "curriculum": {
                "lessons": [
                    {
                        "id": f"lesson-{i}",
                        "title": {"de": "Lektion", "en": "Lesson"},
                        "activities": [{"id": f"unit-{i}", "source": {"kind": "room", "unit_id": f"unit-{i}"}}],
                    }
                    for i in range(6)
                ]
            },
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "daily_limit_policy_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(rooms, "load_catalogue", lambda: content)
    for module in (courses, rooms, course_endpoints):
        monkeypatch.setattr(module, "COURSES", {course.id: course})
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    monkeypatch.setattr(
        daily_limit,
        "read_history_batch",
        AsyncMock(return_value=LearningHistory(attempted_subtask_ids=[], attempted_lecture_bindings=[])),
    )
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(
            return_value=LearningPolicy(mode="daily", premium=False, single_course_sales=False, heart_sales=False)
        ),
    )
    return course


@pytest.fixture
async def daily_client(catalog: Course) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = lambda: USER
    for router in (daily_router, curriculum_endpoints.router, room_router):
        app.include_router(router, dependencies=[Depends(session)])

    @app.exception_handler(daily_limit.AccessError)
    async def access_error(_request: Any, exc: daily_limit.AccessError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder({"code": exc.code, "detail": exc.detail, "daily": exc.daily}),
        )

    async with db_context():
        await daily_limit.configure(
            LimitConfiguration(mode="enforce", limit=3, updated_by="test", note="Synthetic test")
        )
    async with httpx.AsyncClient(
        app=app, base_url="http://synthetic", headers={"Authorization": "Bearer synthetic"}
    ) as client:
        yield client


async def begin(client: httpx.AsyncClient, i: int, request_id: str | None = None) -> httpx.Response:
    return await client.post(
        f"/courses/daily-course/lessons/lesson-{i}/start", json={"request_id": request_id or str(uuid4())}
    )


@pytest.mark.parametrize("policy_enabled", [False, True])
async def test_legacy_off_preserves_progress_without_recording_starts(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch, policy_enabled: bool
) -> None:
    monkeypatch.setattr(settings, "daily_limit_policy_enabled", policy_enabled)
    history = AsyncMock(side_effect=AssertionError("Disabled admission must not read remote history"))
    monkeypatch.setattr(daily_limit, "read_history_batch", history)
    if policy_enabled:
        monkeypatch.setattr(
            daily_limit,
            "policy",
            AsyncMock(
                return_value=LearningPolicy(mode="legacy", premium=False, single_course_sales=True, heart_sales=True)
            ),
        )
    else:
        monkeypatch.setattr(daily_limit, "policy", REAL_POLICY)
    async with db_context():
        await daily_limit.configure(LimitConfiguration(mode="off", limit=3, updated_by="test", note="Flags off"))

    # More than the future limit, plus exact request retries, must remain free
    # of both counter rows and their durable request receipts.
    for i in range(6):
        request_id = str(uuid4())
        for _ in range(2):
            response = await begin(daily_client, i, request_id)
            assert response.status_code == 200 and response.json()["started"] is True
    body = {"request_id": str(uuid4()), "expected_revision": 0, "state": {"draft": "kept after reload"}}
    saved = await daily_client.put("/rooms/unit-0/state?course=daily-course", json=body)
    assert saved.status_code == 200
    repeated = await daily_client.put("/rooms/unit-0/state?course=daily-course", json=body)
    assert repeated.status_code == 200 and repeated.json()["progress"] == saved.json()["progress"]
    reloaded = await daily_client.get("/rooms/unit-0?course=daily-course")
    assert reloaded.status_code == 200 and reloaded.json()["progress"]["state"] == body["state"]
    completed = await daily_client.post(
        "/rooms/unit-0/complete?course=daily-course",
        json={"request_id": str(uuid4()), "expected_revision": 1, "action": "complete", "answer": {"answer": 6}},
    )
    assert completed.status_code == 200 and completed.json()["progress"]["status"] == "completed"
    async with db_context():
        admission = await daily_limit.challenge_admission(
            USER.id,
            ChallengeAdmission(
                task_id=None,
                lecture_bindings=[LectureBinding(course_id=catalog.id, section_id=None, lecture_id=None)],
                request_id=uuid4(),
            ),
            mutate=True,
        )
        assert admission["allowed"] is True and admission["heart_policy"] == "legacy"
        if not policy_enabled:
            assert await daily_limit.backfill_user(USER.id) == 0
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 0
        assert await db.count(filter_by(models.LessonStartRequest, user_id=USER.id)) == 0
        assert await db.count(filter_by(models.PurchaseUser, user_id=USER.id)) == 1
    history.assert_not_awaited()


async def test_disabled_concrete_admission_preserves_course_rights_without_history(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "daily_limit_policy_enabled", False)
    monkeypatch.setattr(daily_limit, "policy", REAL_POLICY)
    history = AsyncMock(side_effect=AssertionError("Disabled admission must not depend on Challenges history"))
    monkeypatch.setattr(daily_limit, "read_history_batch", history)
    owned = AsyncMock(return_value=[])
    premium = AsyncMock(return_value=False)
    monkeypatch.setattr(courses, "get_owned_courses", owned)
    monkeypatch.setattr(shop, "has_premium", premium)
    catalog.curriculum = None
    catalog.sections = [
        Section.parse_obj(
            {
                "id": "legacy-section",
                "title": "Synthetic section",
                "description": None,
                "lectures": [
                    {
                        "id": "legacy-video",
                        "title": "Synthetic video",
                        "description": None,
                        "type": "youtube",
                        "video_id": "synthetic",
                        "duration": 60,
                    }
                ],
            }
        )
    ]
    data = ChallengeAdmission(
        task_id=None,
        lecture_bindings=[LectureBinding(course_id=catalog.id, section_id="legacy-section", lecture_id="legacy-video")],
        request_id=uuid4(),
    )

    async def allowed() -> None:
        for mutate in (False, True):
            async with db_context():
                result = await daily_limit.challenge_admission(USER.id, data, mutate)
                assert result["allowed"] is True and result["heart_policy"] == "legacy"
                assert result["lesson"]["course_id"] == catalog.id and result["daily"] is None

    await allowed()
    catalog.price = 1000
    for mutate in (False, True):
        async with db_context():
            with pytest.raises(HTTPException) as error:
                await daily_limit.challenge_admission(USER.id, data, mutate)
            assert error.value.status_code == 403
    owned.return_value = [catalog.id]
    await allowed()
    owned.return_value = []
    premium.return_value = True
    await allowed()
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 0
        assert await db.count(filter_by(models.LessonStartRequest, user_id=USER.id)) == 0
    history.assert_not_awaited()


async def test_three_starts_retry_and_read_only_browsing(daily_client: httpx.AsyncClient) -> None:
    for _ in range(2):
        assert (await daily_client.get("/daily-limit")).json()["used"] == 0
        assert (await daily_client.get("/courses/daily-course/curriculum")).status_code == 200
        assert (await daily_client.get("/courses/daily-course/lessons/lesson-0")).status_code == 200
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 0
        assert await db.count(filter_by(models.PurchaseUser, user_id=USER.id)) == 0
    request_id = str(uuid4())
    first = await begin(daily_client, 0, request_id)
    assert first.status_code == 200 and first.json()["daily"]["used"] == 1
    assert (await begin(daily_client, 0, request_id)).json() == first.json()
    conflict = await begin(daily_client, 1, request_id)
    assert conflict.status_code == 409 and conflict.json()["code"] == "request_id_conflict"
    assert (await begin(daily_client, 1)).status_code == 200
    assert (await begin(daily_client, 2)).json()["daily"]["remaining"] == 0
    denied = await begin(daily_client, 3)
    assert denied.status_code == 429 and denied.json()["code"] == "daily_limit_reached"
    assert (await begin(daily_client, 0)).status_code == 200
    browsed = await daily_client.get("/courses/daily-course/lessons/lesson-3")
    assert browsed.status_code == 200 and browsed.json()["daily"]["can_start"] is False
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 3


async def test_room_mutation_counts_and_limit_queue_keeps_review(daily_client: httpx.AsyncClient) -> None:
    first = {"request_id": str(uuid4()), "expected_revision": 0, "state": {"draft": "keep me"}}
    saved = await daily_client.put("/rooms/unit-0/state?course=daily-course", json=first)
    assert saved.status_code == 200 and saved.json()["daily"]["used"] == 1
    assert saved.json()["course_id"] == "daily-course" and saved.json()["lesson_id"] == "lesson-0"
    assert (await daily_client.put("/rooms/unit-0/state?course=daily-course", json=first)).json() == saved.json()
    for i in [1, 2]:
        assert (await begin(daily_client, i)).status_code == 200
    next_room = await daily_client.get("/rooms?path=daily-path&course=daily-course")
    assert next_room.json()["next"]["unit"]["id"] == "unit-0"
    denied = await daily_client.put(
        "/rooms/unit-3/state?course=daily-course", json={**first, "request_id": str(uuid4())}
    )
    assert denied.status_code == 429
    assert (await daily_client.get("/rooms/unit-0?course=daily-course")).json()["progress"]["state"] == {
        "draft": "keep me"
    }


async def test_skip_is_free_but_does_not_create_access(daily_client: httpx.AsyncClient) -> None:
    skip = await daily_client.post(
        "/rooms/unit-0/complete?course=daily-course",
        json={"request_id": str(uuid4()), "expected_revision": 0, "action": "skip"},
    )
    assert skip.status_code == 200 and skip.json()["daily"]["used"] == 0
    assert skip.json()["daily"]["started"] is False
    for i in [1, 2, 3]:
        assert (await begin(daily_client, i)).status_code == 200
    denied = await daily_client.post(
        "/rooms/unit-0/review?course=daily-course", json={"request_id": str(uuid4()), "expected_revision": 1}
    )
    assert denied.status_code == 429


@pytest.mark.parametrize(
    "policy_mode,technical", [("shadow", "shadow"), ("legacy", "enforce"), ("daily", "off"), ("daily", "shadow")]
)
async def test_modes_do_not_accidentally_enforce(
    daily_client: httpx.AsyncClient,
    monkeypatch: MonkeyPatch,
    policy_mode: Literal["legacy", "shadow", "daily"],
    technical: Literal["off", "shadow", "enforce"],
) -> None:
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(
            return_value=LearningPolicy(mode=policy_mode, premium=False, single_course_sales=True, heart_sales=True)
        ),
    )
    async with db_context():
        await daily_limit.configure(LimitConfiguration(mode=technical, limit=3, updated_by="test", note="Synthetic"))
    for i in range(5):
        response = await begin(daily_client, i)
        assert response.status_code == 200
    daily = (await daily_client.get("/daily-limit")).json()
    assert daily["mode"] == policy_mode and not daily["enforced"] and not daily["unlimited"]
    assert daily["used"] == (0 if technical == "off" else 5)
    assert daily["remaining"] is None


async def test_premium_expiry_and_real_purchase_not_watch(
    daily_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, catalog: Course
) -> None:
    premium = AsyncMock(
        return_value=LearningPolicy(mode="daily", premium=True, single_course_sales=False, heart_sales=False)
    )
    monkeypatch.setattr(daily_limit, "policy", premium)
    assert (await begin(daily_client, 0)).json()["daily"]["used"] == 0
    premium.return_value = LearningPolicy(mode="daily", premium=False, single_course_sales=False, heart_sales=False)
    for i in [1, 2, 3]:
        assert (await begin(daily_client, i)).status_code == 200
    assert (await begin(daily_client, 0)).status_code == 200
    async with db_context():
        await db.add(models.LastWatch(user_id=USER.id, course_id=catalog.id, timestamp=utcnow()))
    assert (await begin(daily_client, 4)).status_code == 429
    async with db_context():
        await models.CourseAccess.create(USER.id, catalog.id)
    purchased = await begin(daily_client, 4)
    assert purchased.status_code == 200 and purchased.json()["daily"]["exempt"] == "purchase"
    assert purchased.json()["daily"]["used"] == 3


async def test_old_progress_and_account_erasure(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    async with db_context():
        await db.add(
            models.RoomState(
                user_id=USER.id,
                unit_id="unit-0",
                revision=4,
                state={"draft": "old"},
                status="in_progress",
                result=None,
                updated_at=utcnow(),
            )
        )
    for i in [1, 2, 3]:
        assert (await begin(daily_client, i)).status_code == 200
    assert (await begin(daily_client, 0)).status_code == 200
    async with db_context():
        data = await export_user_data(USER.id)
        assert len(data.lesson_starts) == 4 and len(data.lesson_start_requests) == 4
        assert all(row["policy_mode"] == "daily" for row in data.lesson_starts)
        assert next(row for row in data.lesson_starts if row["lesson_id"] == "lesson-0")["reason"] == "historical"
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    monkeypatch.setattr("api.services.retained_rights.preserve_before_erasure", AsyncMock())
    async with db_context():
        await delete_user_data(USER.id)
    assert (await begin(daily_client, 5)).status_code == 401
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 0
        assert await db.count(filter_by(models.LessonStartRequest, user_id=USER.id)) == 0


async def test_entitlement_outage_preserves_work_without_charging(
    daily_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await begin(daily_client, 0)
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "Unavailable")),
    )
    result = await begin(daily_client, 1)
    assert result.status_code == 200 and result.json()["daily"] is None
    assert (await begin(daily_client, 0)).status_code == 200
    assert (await daily_client.get("/daily-limit")).status_code == 503
    async with db_context():
        broad = ChallengeAdmission(
            lecture_bindings=[LectureBinding(course_id="daily-course", lecture_id=None, section_id=None)],
            request_id=uuid4(),
        )
        free_practice = await daily_limit.challenge_admission(USER.id, broad, True)
        assert free_practice["allowed"] and free_practice["daily"] is None
    async with db_context():
        rows = await db.all(filter_by(models.LessonStart, user_id=USER.id))
        assert len(rows) == 2 and sum(row.charged for row in rows) == 1
        assert any(row.reason == "service_unavailable" for row in rows)


@pytest.mark.parametrize(
    "instant,day,reset",
    [
        ("2026-09-26T21:59:59+00:00", "2026-09-26", "2026-09-26T22:00:00+00:00"),
        ("2026-09-26T22:00:00+00:00", "2026-09-27", "2026-09-27T22:00:00+00:00"),
        ("2026-10-24T22:00:00+00:00", "2026-10-25", "2026-10-25T23:00:00+00:00"),
        ("2027-03-27T23:00:00+00:00", "2027-03-28", "2027-03-28T22:00:00+00:00"),
    ],
)
def test_berlin_boundaries_and_dst(instant: str, day: str, reset: str) -> None:
    actual_day, actual_reset = daily_limit.day_window(datetime.fromisoformat(instant))
    assert str(actual_day) == day and actual_reset.isoformat() == reset


async def test_midnight_resets_only_new_starts(daily_client: httpx.AsyncClient, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(daily_limit, "utcnow", lambda: datetime(2026, 9, 26, 21, 59, tzinfo=timezone.utc))
    for i in range(3):
        await begin(daily_client, i)
    assert (await begin(daily_client, 3)).status_code == 429
    monkeypatch.setattr(daily_limit, "utcnow", lambda: datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc))
    assert (await begin(daily_client, 0)).json()["daily"]["used"] == 0
    assert (await begin(daily_client, 3)).json()["daily"]["used"] == 1


async def test_activation_requires_curated_curriculum(catalog: Course) -> None:
    catalog.curriculum = None
    async with db_context():
        with pytest.raises(HTTPException) as failure:
            await daily_limit.configure(
                LimitConfiguration(mode="enforce", limit=3, updated_by="test", note="Synthetic")
            )
        assert getattr(failure.value, "status_code", None) == 409
        assert isinstance(failure.value.detail, dict)
        assert "curriculum_required:daily-course" in failure.value.detail["issues"]


async def test_grouping_preserves_old_unit_urls_and_begun_lessons(
    daily_client: httpx.AsyncClient, catalog: Course
) -> None:
    async with db_context():
        now = utcnow()
        await db.add(
            models.LessonStart(
                user_id=USER.id,
                course_id=catalog.id,
                lesson_id="unit-0",
                started_at=now,
                local_day=daily_limit.day_window(now)[0],
                charged=False,
                reason="off",
            )
        )
    assert catalog.curriculum is not None
    catalog.curriculum.lessons[0].activities += catalog.curriculum.lessons[1].activities
    catalog.curriculum.lessons.pop(1)
    alias = await daily_client.get("/courses/daily-course/lessons/unit-1")
    assert alias.status_code == 200 and alias.json()["id"] == "lesson-0"
    assert alias.json()["initial_activity_id"] == "unit-1"
    assert alias.json()["daily"]["started"] is True
    result = await daily_client.post("/courses/daily-course/lessons/unit-1/start", json={"request_id": str(uuid4())})
    assert result.status_code == 200 and result.json()["daily"]["used"] == 0


async def test_challenge_read_browses_but_start_enforces(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    task, subtask = uuid4(), uuid4()
    value = rooms.load_catalogue()
    value.units[0].completion = None
    value.units[0].room = "exercise"
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    for i in [1, 2, 3]:
        await begin(daily_client, i)
    data = ChallengeAdmission(task_id=task, subtask_id=subtask, request_id=uuid4())
    async with db_context():
        result = await daily_limit.challenge_admission(USER.id, data, False)
        assert result["allowed"] is True and result["daily"].can_start is False
    async with db_context():
        with pytest.raises(daily_limit.AccessError) as failure:
            await daily_limit.challenge_admission(USER.id, data, True)
        assert failure.value.status_code == 429
    async with db_context():
        broad = ChallengeAdmission(
            lecture_bindings=[LectureBinding(course_id=catalog.id, lecture_id=None, section_id=None)],
            request_id=uuid4(),
        )
        assert (await daily_limit.challenge_admission(USER.id, broad, False))["allowed"]
        free_practice = await daily_limit.challenge_admission(USER.id, broad, True)
        assert free_practice["allowed"] is True and free_practice["lesson"] is None
    async with db_context():
        unknown = ChallengeAdmission(
            lecture_bindings=[LectureBinding(course_id="invented", lecture_id=None, section_id=None)],
            request_id=uuid4(),
        )
        with pytest.raises(HTTPException) as unknown_error:
            await daily_limit.challenge_admission(USER.id, unknown, False)
        assert unknown_error.value.status_code == 404


async def test_historical_backfill_is_idempotent_and_excludes_skips(daily_client: httpx.AsyncClient) -> None:
    async with db_context():
        for i, state in [(0, "completed"), (1, "in_progress"), (2, "skipped")]:
            await db.add(
                models.RoomState(
                    user_id=USER.id,
                    unit_id=f"unit-{i}",
                    revision=1,
                    status=state,
                    state={},
                    result=None,
                    updated_at=utcnow(),
                )
            )
    async with db_context():
        assert await daily_limit.backfill_user(USER.id) == 2
    async with db_context():
        assert await daily_limit.backfill_user(USER.id) == 0
        records = await db.all(filter_by(models.LessonStart, user_id=USER.id))
        assert {r.lesson_id for r in records} == {"lesson-0", "lesson-1"}
        assert not any(r.charged for r in records)


async def test_admin_is_exempt_and_grouped_activities_count_once(
    daily_client: httpx.AsyncClient, catalog: Course
) -> None:
    assert catalog.curriculum is not None
    catalog.curriculum.lessons[0].activities += catalog.curriculum.lessons[1].activities
    catalog.curriculum.lessons.pop(1)
    for i in [0, 1]:
        response = await daily_client.put(
            f"/rooms/unit-{i}/state?course=daily-course",
            json={"request_id": str(uuid4()), "expected_revision": 0, "state": {"edited": True}},
        )
        assert response.status_code == 200 and response.json()["daily"]["used"] == 1
    async with db_context():
        admin = User(id="admin", admin=True, email_verified=True)
        for lesson in catalog.curriculum.lessons:
            daily = await daily_limit.start(admin, catalog, lesson)
            assert daily is not None and daily.unlimited and daily.used == 0


@pytest.mark.parametrize(
    "body,status_code,expected",
    [
        ({"mode": "daily", "premium": False, "single_course_sales": False, "heart_sales": False}, 200, 200),
        ({"mode": "daily", "premium": "false", "single_course_sales": False, "heart_sales": False}, 200, 503),
        ({}, 500, 503),
        ({}, 404, 401),
    ],
)
async def test_backend_policy_never_turns_an_error_into_nonpremium(
    monkeypatch: MonkeyPatch, body: Any, status_code: int, expected: int
) -> None:
    # This test uses the real HTTP decoder, not the fixture's replaced function.
    from importlib import import_module
    from types import SimpleNamespace

    real_policy = import_module("api.services.daily_limit").policy
    requests = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body)

    monkeypatch.setattr(settings, "daily_limit_policy_enabled", True)
    monkeypatch.setattr(
        daily_limit,
        "InternalService",
        SimpleNamespace(
            SHOP=SimpleNamespace(
                client=httpx.AsyncClient(base_url="http://policy.synthetic", transport=httpx.MockTransport(reply))
            )
        ),
    )
    async with db_context():
        if expected == 200:
            value = await real_policy(USER.id)
            assert value.mode == "daily" and value.premium is False
            assert await real_policy(USER.id) is value
        else:
            with pytest.raises(HTTPException) as failure:
                await real_policy(USER.id)
            assert failure.value.status_code == expected
            if expected == 503:
                with pytest.raises(HTTPException):
                    await real_policy(USER.id)
        assert len(requests) == 1


async def test_disabled_feature_does_not_require_curriculum_for_legacy_video(
    catalog: Course, monkeypatch: MonkeyPatch, tmp_path: Any
) -> None:
    from api.schemas.course import Mp4Lecture

    lecture = Mp4Lecture(id="video", title="Synthetic", description=None, duration=60)
    monkeypatch.setattr(settings, "daily_limit_policy_enabled", False)
    monkeypatch.setattr(settings, "mp4_lectures", tmp_path)
    (tmp_path / catalog.id).mkdir()
    (tmp_path / catalog.id / "video.mp4").write_bytes(b"synthetic")
    monkeypatch.setattr(redis, "setex", AsyncMock())
    monkeypatch.setattr(course_endpoints, "clear_cache", AsyncMock())

    def no_curriculum(*_args: Any) -> Any:
        raise AssertionError("The disabled feature must not need a room catalogue")

    monkeypatch.setattr(daily_limit, "lecture_lesson", no_curriculum)
    async with db_context():
        assert "/lectures/" in await course_endpoints.get_mp4_lecture_link(catalog, lecture, USER)
        assert await course_endpoints.complecte_lecture(course=catalog, lecture=lecture, user=USER) is True


@pytest.mark.parametrize("entrypoint", ["start", "direct", "complete", "backfill"])
async def test_historical_challenge_attempt_preserves_canonical_lesson(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch, entrypoint: str
) -> None:
    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[0].completion = None
    rooms.load_catalogue().units[0].room = "exercise"
    history = AsyncMock(return_value=LearningHistory(attempted_subtask_ids=[subtask], attempted_lecture_bindings=[]))
    monkeypatch.setattr(daily_limit, "read_history_batch", history)
    # The trusted participation evidence applies even when the earlier attempt
    # failed; it grants continuation, never completion or XP.
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    for i in [1, 2, 3]:
        assert (await begin(daily_client, i)).status_code == 200
    response = await daily_client.get("/courses/daily-course/lessons/lesson-0")
    assert response.status_code == 200 and response.json()["daily"]["started"]
    queue = await daily_client.get("/rooms?path=daily-path&continuous=true")
    assert queue.status_code == 200 and queue.json()["next"]["unit"]["id"] == "unit-0"
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 3
    if entrypoint == "start":
        assert (await begin(daily_client, 0)).status_code == 200
    elif entrypoint == "direct":
        async with db_context():
            result = await daily_limit.challenge_admission(
                USER.id, ChallengeAdmission(task_id=task, subtask_id=subtask, request_id=uuid4()), True
            )
            assert result["allowed"] and result["daily"].used == 3
    elif entrypoint == "complete":
        monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))
        completed_response = await daily_client.post(
            "/rooms/unit-0/complete?course=daily-course",
            json={"request_id": str(uuid4()), "expected_revision": 0, "action": "complete"},
        )
        assert completed_response.status_code == 200, completed_response.text
    else:
        async with db_context():
            assert await daily_limit.backfill_user(USER.id) == 1
            assert await daily_limit.backfill_user(USER.id) == 0
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and row.reason == "historical" and not row.charged


async def test_policy_outage_preserves_only_proven_paid_course_work(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    catalog.price = 1000
    for module in (course_endpoints, rooms):
        monkeypatch.setattr(module, "has_premium", AsyncMock(return_value=False))
    assert (await begin(daily_client, 0)).status_code == 200
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "Unavailable")),
    )
    for path in ["/courses/daily-course/lessons/lesson-0", "/rooms/unit-0?course=daily-course"]:
        result = await daily_client.get(path)
        assert result.status_code == 200 and result.json()["daily"] is None, result.text
    assert (await begin(daily_client, 0)).status_code == 200
    for path in ["/courses/daily-course/lessons/lesson-1", "/rooms/unit-1?course=daily-course"]:
        assert (await daily_client.get(path)).status_code == 503
    assert (await begin(daily_client, 1)).status_code == 503
    queue = await daily_client.get("/rooms?path=daily-path&course=daily-course&after=unit-0&continuous=true")
    assert queue.status_code == 200 and queue.json()["next"]["unit"]["id"] == "unit-0", queue.text
    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[0].completion = None
    rooms.load_catalogue().units[0].room = "exercise"
    async with db_context():
        admission = await daily_limit.challenge_admission(
            USER.id, ChallengeAdmission(task_id=task, subtask_id=subtask, request_id=uuid4()), True
        )
        assert admission["allowed"] and admission["daily"] is None
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 1


async def test_historical_attempts_follow_grouping_and_legacy_lectures(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    from api.schemas.course import Section
    from api.schemas.daily_limit import HistoryLecture

    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-4": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[4].completion = None
    rooms.load_catalogue().units[4].room = "exercise"
    assert catalog.curriculum is not None
    catalog.curriculum.lessons[0].activities += catalog.curriculum.lessons[4].activities
    catalog.curriculum.lessons.pop(4)
    history = AsyncMock(
        return_value=LearningHistory(
            attempted_subtask_ids=[subtask],
            attempted_lecture_bindings=[HistoryLecture(course_id=catalog.id, lecture_id="old-video")],
        )
    )
    monkeypatch.setattr(daily_limit, "read_history_batch", history)
    alias = await daily_client.get("/courses/daily-course/lessons/unit-4")
    assert alias.status_code == 200 and alias.json()["id"] == "lesson-0"
    assert alias.json()["initial_activity_id"] == "unit-4" and alias.json()["daily"]["started"]
    async with db_context():
        assert await daily_limit.backfill_user(USER.id) == 1
        assert await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert not await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="unit-4")
    catalog.curriculum = None
    catalog.learning_path_id = None
    catalog.sections = [
        Section.parse_obj(
            {
                "id": "old-section",
                "title": "Old",
                "lectures": [
                    {
                        "id": "old-video",
                        "type": "youtube",
                        "title": "Old",
                        "description": None,
                        "duration": 60,
                        "video_id": "abcdefghijk",
                    }
                ],
            }
        )
    ]
    async with db_context():
        lesson = daily_limit.lecture_lesson(catalog, "old-video")
        assert await daily_limit.begun(USER, catalog, lesson)
        payload = history.call_args.args[1]
        assert {"course_id": catalog.id, "lecture_id": "old-video"} in payload["lecture_bindings"]


@pytest.mark.parametrize("reply", ["valid", "unrequested", "unavailable", "malformed"])
async def test_history_decoder_requires_trusted_requested_evidence(monkeypatch: MonkeyPatch, reply: str) -> None:
    from types import SimpleNamespace

    subtask = uuid4()
    payload = {"subtask_ids": [str(subtask)], "lecture_bindings": [{"course_id": "course", "lecture_id": "lecture"}]}
    body = {
        "attempted_subtask_ids": [str(subtask if reply != "unrequested" else uuid4())],
        "attempted_lecture_bindings": payload["lecture_bindings"],
    }
    if reply == "malformed":
        body = {}

    def response(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/users/daily-user/learning-history"
        return httpx.Response(500 if reply == "unavailable" else 200, json=body)

    monkeypatch.setattr(
        daily_limit,
        "InternalService",
        SimpleNamespace(
            CHALLENGES=SimpleNamespace(
                client=httpx.AsyncClient(base_url="http://history.synthetic", transport=httpx.MockTransport(response))
            )
        ),
    )
    if reply == "valid":
        result = await daily_limit.read_history_batch(USER.id, payload)
        assert result.attempted_subtask_ids == [subtask]
    else:
        with pytest.raises(daily_limit.AccessError) as failure:
            await daily_limit.read_history_batch(USER.id, payload)
        assert failure.value.status_code == 503


async def test_history_outage_keeps_known_continuation_and_premium_without_guessing(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    assert (await begin(daily_client, 3)).status_code == 200
    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[0].completion = None
    rooms.load_catalogue().units[0].room = "exercise"
    history = AsyncMock(side_effect=daily_limit.AccessError(503, "learning_history_unavailable", "Unavailable"))
    monkeypatch.setattr(daily_limit, "read_history_batch", history)
    assert (await begin(daily_client, 0)).status_code == 503
    catalog.price = 1000
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "Unavailable")),
    )
    # Course browsing finds the locally known later lesson before consulting
    # unavailable history for the first exercise in the curriculum.
    assert (await daily_client.get("/courses/daily-course/curriculum")).status_code == 200
    assert (await begin(daily_client, 3)).status_code == 200
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(
            return_value=LearningPolicy(mode="daily", premium=True, single_course_sales=False, heart_sales=False)
        ),
    )
    assert (await begin(daily_client, 0)).status_code == 200
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and not row.charged and row.reason == "premium"


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_non_enforcing_mode_does_not_block_on_history_outage(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch, mode: str
) -> None:
    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[0].completion = None
    rooms.load_catalogue().units[0].room = "exercise"
    monkeypatch.setattr(
        daily_limit,
        "read_history_batch",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_history_unavailable", "Unavailable")),
    )
    async with db_context():
        await daily_limit.configure(
            LimitConfiguration.parse_obj({"mode": mode, "limit": 3, "updated_by": "test", "note": "No enforcement"})
        )
    result = await begin(daily_client, 0)
    assert result.status_code == 200 and result.json()["daily"]["started"], result.text
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and not row.charged


@pytest.mark.parametrize(
    "policy_mode,technical,premium,expected",
    [
        ("daily", "enforce", True, "daily"),
        ("daily", "off", False, "daily"),
        ("daily", "shadow", False, "daily"),
        ("shadow", "shadow", False, "legacy"),
        ("legacy", "enforce", False, "legacy"),
    ],
)
async def test_heart_policy_outage_requires_confirmed_lesson_contract(
    daily_client: httpx.AsyncClient,
    catalog: Course,
    monkeypatch: MonkeyPatch,
    policy_mode: str,
    technical: str,
    premium: bool,
    expected: str,
) -> None:
    known = LearningPolicy.parse_obj(
        {"mode": policy_mode, "premium": premium, "single_course_sales": False, "heart_sales": False}
    )
    monkeypatch.setattr(daily_limit, "policy", AsyncMock(return_value=known))
    async with db_context():
        await daily_limit.configure(
            LimitConfiguration.parse_obj({"mode": technical, "limit": 3, "updated_by": "test", "note": "Synthetic"})
        )
    assert (await begin(daily_client, 0)).status_code == 200
    task, subtask = uuid4(), uuid4()
    monkeypatch.setattr(
        settings,
        "learning_rooms_exercise_refs",
        {"unit-0": {"type": "coding", "task_id": str(task), "subtask_id": str(subtask)}},
    )
    rooms.load_catalogue().units[0].completion = None
    rooms.load_catalogue().units[0].room = "exercise"
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "Unavailable")),
    )
    async with db_context():
        result = await daily_limit.challenge_admission(
            USER.id, ChallengeAdmission(task_id=task, subtask_id=subtask), False
        )
        assert result["allowed"] and result["daily"] is None and result["heart_policy"] == expected
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and row.policy_mode == policy_mode
        assert await db.count(filter_by(models.LessonStart, user_id=USER.id)) == 1
        assert (await daily_limit.challenge_admission(USER.id, ChallengeAdmission(), False))["heart_policy"] is None
        broad = ChallengeAdmission(
            lecture_bindings=[LectureBinding(course_id=catalog.id, lecture_id=None, section_id=None)]
        )
        assert (await daily_limit.challenge_admission(USER.id, broad, False))["heart_policy"] is None


async def test_unknown_old_start_is_not_a_daily_billing_claim(
    daily_client: httpx.AsyncClient, catalog: Course, monkeypatch: MonkeyPatch
) -> None:
    assert (await begin(daily_client, 0)).status_code == 200
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None
        row.reason = "premium"
        row.policy_mode = None
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "Unavailable")),
    )
    async with db_context():
        pair = catalog, daily_limit.lesson_definition(catalog, "lesson-0")
        assert await daily_limit.begun(USER, *pair)
        assert await daily_limit.heart_policy(USER, pair) is None
    monkeypatch.setattr(
        daily_limit,
        "policy",
        AsyncMock(
            return_value=LearningPolicy(mode="daily", premium=False, single_course_sales=False, heart_sales=False)
        ),
    )
    # Read-only status cannot manufacture persistent evidence; a subsequent
    # deliberate admitted start can remember the freshly verified contract.
    assert (await daily_client.get("/courses/daily-course/lessons/lesson-0")).status_code == 200
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and row.policy_mode is None
    assert (await begin(daily_client, 0)).status_code == 200
    async with db_context():
        row = await db.get(models.LessonStart, user_id=USER.id, course_id=catalog.id, lesson_id="lesson-0")
        assert row is not None and row.policy_mode == "daily"
