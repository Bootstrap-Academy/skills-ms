"""Real retained-learning route guards with committed rights and synthetic media."""

import json
from pathlib import Path
from typing import Any, AsyncIterator, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI
from pytest import MonkeyPatch

from api import models
from api.database import db, db_context, filter_by
from api.endpoints import course as course_routes
from api.endpoints import learning
from api.redis import redis
from api.schemas.course import Course
from api.schemas.user import User
from api.services import courses
from api.settings import settings


@pytest.mark.parametrize("enabled", [False, True])
async def test_retained_course_and_media_use_real_course_guard(
    monkeypatch: MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    user = User(id="retained-subject", email_verified=True, admin=False)
    course = Course.parse_obj(
        {
            "id": "retained-course",
            "title": "Synthetic",
            "price": 1000,
            "description": None,
            "category": None,
            "language": "en",
            "image": None,
            "authors": [],
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            "sections": [
                {
                    "id": "section",
                    "title": "Section",
                    "lectures": [{"id": "video", "title": "Video", "description": None, "duration": 10, "type": "mp4"}],
                }
            ],
        }
    )
    for module in (course_routes, learning, courses):
        monkeypatch.setattr(module, "COURSES", {course.id: course})
    monkeypatch.setattr(settings, "daily_limit_policy_enabled", enabled)
    monkeypatch.setattr(settings, "mp4_lectures", tmp_path)
    directory = tmp_path / course.id
    directory.mkdir()
    media = b"synthetic-media"
    (directory / "video.mp4").write_bytes(media)
    monkeypatch.setattr(learning, "learning_subject", AsyncMock(return_value=user))
    monkeypatch.setattr(course_routes, "has_premium", AsyncMock(side_effect=AssertionError("Owned retained right")))
    cache: dict[str, str] = {}

    async def get(key: str) -> str | None:
        return cache.get(key)

    async def setex(key: str, ttl: int, value: str) -> None:
        cache[key] = value

    monkeypatch.setattr(redis, "get", get)
    monkeypatch.setattr(redis, "setex", setex)
    async with db_context():
        await models.CourseAccess.create(user.id, course.id)

    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    app = FastAPI()
    app.include_router(learning.router, dependencies=[Depends(session)])
    transport = httpx.ASGITransport(app=cast(Any, app), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://retained.synthetic", headers={"x-learning-key": "x" * 43}
    ) as client:
        details = await client.get(f"/learning/courses/{course.id}")
        assert details.status_code == 200, details.text
        unseen = await client.get(f"/learning/courses/{course.id}/next_unseen")
        assert unseen.status_code == 200 and unseen.json()["lecture"]["id"] == "video"
        link = await client.get(f"/learning/courses/{course.id}/lectures/video")
        assert link.status_code == 200, link.text
        stream_path = httpx.URL(link.json()).path
        stream = await client.get(stream_path, headers={"Range": "bytes=0-3"})
        assert stream.status_code == 206 and stream.content == media[:4], stream.text
        assert stream.headers["Cache-Control"] == "no-store"
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id=user.id)) == 0
    stored = next(json.loads(value) for key, value in cache.items() if key.startswith("learning_mp4:"))
    assert stored["subject"] == user.id
