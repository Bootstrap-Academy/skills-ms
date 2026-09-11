"""Routed stream boundary checks with synthetic files and explicit admission stubs.

No database, Redis service or backend is used. The existing paired HTTP artifact
separately establishes CP1 with real personal/learning/course admission.
"""

import json
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient
from pytest import MonkeyPatch

from api.endpoints import course as courses
from api.endpoints import learning
from api.redis import redis
from api.schemas.user import User
from api.settings import settings


DATA = bytes(index % 256 for index in range(2225))
URL = "/learning/lectures/synthetic-token/course_lecture.mp4"


@pytest.fixture(autouse=True)
def database() -> None:
    """Override the suite's DB fixture: this isolated router does not use a DB."""


@pytest.fixture
async def stream_fixture(tmp_path: Path, monkeypatch: MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    path = tmp_path / "lecture.mp4"
    path.write_bytes(DATA)
    user = User(id="synthetic-learning-subject", email_verified=True, admin=False)
    course = object()
    record = {"path": str(path), "subject": user.id, "course": "course", "authority": "synthetic-digest"}
    redis_get = AsyncMock(return_value=json.dumps(record))
    authority = AsyncMock(return_value=user)
    admission = AsyncMock()
    monkeypatch.setattr(redis, "get", redis_get)
    monkeypatch.setattr(learning, "learning_subject", authority)
    monkeypatch.setattr(learning, "COURSES", {"course": course})
    monkeypatch.setattr(courses.has_course_access, "dependency", admission)
    monkeypatch.setattr(settings, "stream_chunk_size", 64)
    app = FastAPI()
    app.include_router(learning.router)
    async with AsyncClient(app=app, base_url="http://stream.synthetic") as client:
        yield {
            "client": client,
            "path": path,
            "user": user,
            "course": course,
            "record": record,
            "redis": redis_get,
            "authority": authority,
            "admission": admission,
        }


@pytest.mark.parametrize(
    "range_header,start,end",
    [
        (None, 0, 64),
        ("bytes=0-31", 0, 32),
        ("bytes=64-95", 64, 96),
        ("bytes=0-0", 0, 1),
        ("bytes=2224-2224", 2224, 2225),
        ("bytes=2224-", 2224, 2225),
        ("bytes=2200-9999", 2200, 2225),
        ("bytes=64-", 64, 128),
        ("bytes=0-9999999999999999", 0, 2225),
    ],
)
async def test_stream_valid_range(
    stream_fixture: dict[str, Any], range_header: str | None, start: int, end: int
) -> None:
    f = stream_fixture
    response = await f["client"].get(URL, headers={"Range": range_header} if range_header else {})
    assert response.status_code == 206
    assert response.content == DATA[start:end]
    assert response.headers["Content-Range"] == f"bytes {start}-{end - 1}/{len(DATA)}"
    assert response.headers["Content-Length"] == str(end - start)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Accept-Ranges"] == "bytes"
    f["redis"].assert_awaited_once_with("learning_mp4:synthetic-token:course_lecture.mp4")
    f["authority"].assert_awaited_once_with("synthetic-digest")
    f["admission"].assert_awaited_once_with(course=f["course"], user=f["user"])


@pytest.mark.parametrize(
    "range_header,empty",
    [
        ("bytes=2225-", False),
        ("bytes=2245-", False),
        ("bytes=2225-2245", False),
        ("bytes=64-31", False),
        ("bytes=1-0", False),
        ("bytes=0-", True),
        ("bytes=0-0", True),
    ],
)
async def test_stream_rejects_unsatisfiable_or_reversed_range(
    stream_fixture: dict[str, Any], range_header: str, empty: bool
) -> None:
    f = stream_fixture
    if empty:
        f["path"].write_bytes(b"")
    response = await f["client"].get(URL, headers={"Range": range_header})
    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["Content-Range"] == f"bytes */{0 if empty else len(DATA)}"
    assert response.headers["Content-Length"] == "0"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Accept-Ranges"] == "bytes"
    f["authority"].assert_awaited_once_with("synthetic-digest")
    f["admission"].assert_awaited_once_with(course=f["course"], user=f["user"])


@pytest.mark.parametrize(
    "refusal,status", [("token", 404), ("authority", 401), ("subject", 404), ("course", 403), ("file", 404)]
)
async def test_stream_checks_existing_admission_before_range(
    stream_fixture: dict[str, Any], refusal: str, status: int
) -> None:
    f = stream_fixture
    if refusal == "token":
        f["redis"].return_value = None
    elif refusal == "authority":
        f["authority"].side_effect = HTTPException(401, "Synthetic revoked learning key")
    elif refusal == "subject":
        f["authority"].return_value = User(id="other-subject", email_verified=True, admin=False)
    elif refusal == "course":
        f["admission"].side_effect = HTTPException(403, "Synthetic withdrawn course")
    elif refusal == "file":
        f["path"].unlink()
    response = await f["client"].get(URL, headers={"Range": "bytes=2245-"})
    assert response.status_code == status
    assert "Content-Range" not in response.headers
    if refusal == "token":
        f["authority"].assert_not_awaited()
    if refusal in ("token", "authority", "subject"):
        f["admission"].assert_not_awaited()


@pytest.mark.parametrize("range_header", ["bytes=-32", "bytes=0-1,5-8", "bytes=x-3", "bytes=10000000000000000-"])
async def test_stream_preserves_existing_header_validation(stream_fixture: dict[str, Any], range_header: str) -> None:
    response = await stream_fixture["client"].get(URL, headers={"Range": range_header})
    assert response.status_code == 422
    stream_fixture["redis"].assert_not_awaited()
