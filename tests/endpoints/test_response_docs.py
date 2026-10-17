"""The routes of learning rooms, curriculum, daily limit and lesson engine document every answer they give.

With `DEBUG` set, `api.utils.debug.check_responses` logs an undocumented or mismatching answer at ERROR. The pairs
below are the ones it reported on a test deployment before these routes documented them.
"""

import json
from typing import Any, cast

import httpx
import pytest
from _pytest.monkeypatch import MonkeyPatch
from fastapi.exception_handlers import http_exception_handler
from fastapi.routing import APIRoute, iter_route_contexts
from pytest_mock import MockerFixture
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from api.app import app
from api.exceptions.engine import EngineRefusal
from api.schemas.engine import RevisionPublished
from api.settings import settings
from api.utils.debug import _check_response_schema, _documented, check_responses

START = "/engine/units/{unit_id}/tasks/{goal_id}/start"
SUBMISSIONS = "/engine/units/{unit_id}/tasks/{goal_id}/submissions"
INVALID_TOKEN = {"detail": "Invalid token"}

SEEN = [
    ("GET", "/daily-limit", 200),
    ("GET", "/rooms/capabilities", 200),
    ("GET", "/rooms", 200),
    ("GET", "/courses/{course_id}/lessons/{lesson_id}", 200),
    ("GET", "/courses/{course_id}/curriculum", 200),
    ("PUT", "/rooms/{unit_id}/state", 200),
    ("GET", "/rooms/{unit_id}", 200),
    ("POST", START, 200),
    ("POST", SUBMISSIONS, 200),
    ("POST", "/learning-access/{user_id}/check", 200),
    ("GET", "/rooms", 401),
    ("GET", "/engine/submissions/{assessment_id}", 200),
    ("POST", START, 409),
    ("POST", "/engine/admin/revisions", 200),
    ("POST", "/learning-access/{user_id}/check-batch", 200),
    ("POST", "/engine/admin/revisions", 401),
    ("PUT", "/rooms/{unit_id}/state", 409),
    ("POST", "/engine/admin/revisions", 201),
    ("POST", SUBMISSIONS, 202),
    ("POST", "/engine/admin/revisions", 404),
    ("GET", "/daily-limit", 401),
    ("POST", "/rooms/{unit_id}/review", 409),
    ("POST", "/rooms/{unit_id}/complete", 200),
    ("POST", "/learning-access/{user_id}/start", 200),
    ("POST", SUBMISSIONS, 409),
]


def find(method: str, path: str) -> APIRoute:
    """The route as the middleware sees it (`scope["route"]` is the route of the included router). `GET /daily-limit`
    exists twice; the learner's route comes before the internal configuration of the same path."""
    found = [
        route
        for context in iter_route_contexts(app.routes)
        if isinstance(route := context.original_route, APIRoute)
        and route.path == path
        and method in (route.methods or ())
    ]
    found.sort(key=lambda route: route.endpoint.__module__.startswith("api.endpoints.internal."))
    assert found, (method, path)
    return found[0]


@pytest.mark.parametrize("method,path,status", SEEN)
def test_every_answer_seen_is_documented(method: str, path: str, status: int) -> None:
    assert _documented(find(method, path), status) is not None


def body(value: Any) -> bytes:
    return value if isinstance(value, bytes) else json.dumps(value).encode()


@pytest.mark.parametrize(
    "method,path,status,answer",
    [
        ("GET", "/rooms", 401, INVALID_TOKEN),
        ("GET", "/daily-limit", 401, INVALID_TOKEN),
        ("POST", "/engine/admin/revisions", 401, INVALID_TOKEN),
        ("PUT", "/rooms/{unit_id}/state", 409, {"detail": "Your room has changed in another session"}),
        ("POST", "/rooms/{unit_id}/review", 409, {"detail": "This room is already in progress"}),
        ("POST", "/engine/admin/revisions", 404, {"detail": "There is no unit of the lesson engine with this id"}),
        (
            "POST",
            "/engine/admin/revisions",
            201,
            RevisionPublished(revision_id="r", number=2, active=True, created=True).model_dump(),
        ),
        (
            "POST",
            START,
            409,
            EngineRefusal(409, "pending", "A submission of this task is still being checked", extra={"assessments": []})
            .response()
            .body,
        ),
        ("POST", SUBMISSIONS, 202, {"assessmentId": "a", "goalId": "g", "status": "pending"}),
        ("POST", SUBMISSIONS, 409, EngineRefusal(409, "conflict", "Another submission came first").response().body),
        # The body `rollback_on_exception` (api/app.py) builds for an AccessError.
        ("GET", "/daily-limit", 503, {"detail": "unavailable", "code": "learning_access_unavailable", "daily": None}),
        ("POST", "/learning-access/{user_id}/check-batch", 401, INVALID_TOKEN),
        (
            "POST",
            "/learning-access/{user_id}/check-batch",
            503,
            {"detail": "unavailable", "code": "learning_access_unavailable", "daily": None},
        ),
    ],
)
def test_the_error_and_accepted_answers_match_their_documentation(
    method: str, path: str, status: int, answer: Any, mocker: MockerFixture
) -> None:
    logger = mocker.patch("api.utils.debug.logger")

    _check_response_schema(method, find(method, path), status, body(answer))

    logger.error.assert_not_called()


@pytest.mark.parametrize(
    "method,url,flags,status",
    [
        ("GET", "/rooms/capabilities", {}, 200),
        ("GET", "/rooms", {"rooms_enabled": True}, 401),
        ("GET", "/rooms", {"rooms_enabled": False}, 404),
        ("GET", "/daily-limit", {}, 401),
        ("GET", "/courses/softskills/curriculum", {}, 401),
        ("POST", "/engine/admin/revisions", {"engine_enabled": True}, 401),
        ("POST", "/engine/admin/revisions", {"engine_enabled": False}, 404),
        ("POST", "/_internal/learning-access/someone/check-batch", {}, 401),
    ],
)
async def test_real_answers_pass_the_debug_check(
    method: str, url: str, flags: dict[str, bool], status: int, mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    for name, value in flags.items():
        monkeypatch.setattr(settings, name, value)
    # tests/conftest.py removes the app's handler, and the errors then come as plain text, which the check skips.
    # Deployed, `rollback_on_exception` answers them with `http_exception_handler`, as JSON.
    monkeypatch.setitem(app.exception_handlers, StarletteHTTPException, http_exception_handler)
    monkeypatch.setattr(app, "middleware_stack", app.build_middleware_stack())
    logger = mocker.patch("api.utils.debug.logger")
    # As `app.middleware("http")(check_responses)` installs it when DEBUG is set.
    checked = BaseHTTPMiddleware(app, dispatch=cast(Any, check_responses))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=checked), base_url="http://test") as client:
        response = await client.request(method, url, json={} if method == "POST" else None)

    assert (response.status_code, response.headers["content-type"]) == (status, "application/json")
    logger.error.assert_not_called()
