"""Fixtures of the lesson engine's endpoint tests: a catalogue with a unit of the engine and an app client."""

from typing import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pytest import MonkeyPatch

from api import models
from api.app import request_validation_error_response
from api.auth import user_auth
from api.database import db, db_context
from api.endpoints import engine as engine_endpoints
from api.endpoints import rooms as room_endpoints
from api.exceptions.api_exception import CodedAPIException
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import engine as engine_service
from api.services import rooms
from api.settings import settings
from tests.endpoints.engine.support import ADMIN, ENGINE_UNIT, LEARNER, ORIGIN, unit
from tests.engine_documents import SUBSKILL, UNIT


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
            # What the middleware of the app does for every request (Ä38 b).
            await engine_service.load_active_units()
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
