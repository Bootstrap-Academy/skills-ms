"""Trusted module publication, origin policy and additive registry migration."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pytest import MonkeyPatch
from sqlalchemy import create_engine, text

from api.database import db_context
from api.schemas.lesson_module import LessonModuleDescriptor
from api.schemas.rooms import CatalogueUnit
from api.services.lesson_modules import checked_descriptor, register_module, resolve_module
from api.settings import settings


@pytest.mark.parametrize("module_id", ["module.name", "module_name", "m" * 81])
def test_registry_and_content_reject_ids_the_browser_loader_cannot_load(module_id: str) -> None:
    descriptor = {"id": "module-" + "m" * 73, "api_version": 1, "entry_url": "https://modules.example/main.mjs"}
    unit = {
        "id": "custom-introduction",
        "path_id": "course",
        "title": {"de": "Einführung", "en": "Introduction"},
        "room": "custom",
        "content": {},
        "teaches": [],
        "practices": [],
        "requires": [],
        "retired": False,
        "completion": {"kind": "introduced", "answer": {"done": True}},
        "module_id": descriptor["id"],
    }
    # The same maximum-length hyphenated ID survives publication and content loading.
    assert LessonModuleDescriptor.parse_obj(descriptor).id == CatalogueUnit.parse_obj(unit).module_id
    with pytest.raises(ValidationError):
        LessonModuleDescriptor.parse_obj({**descriptor, "id": module_id})
    with pytest.raises(ValidationError):
        CatalogueUnit.parse_obj({**unit, "module_id": module_id})


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:password@modules.example/main.mjs",
        "https://modules.example/main.mjs?token=x",
        "https://modules.example/main.mjs#new",
        "https://modules.example/%2e%2e/main.mjs",
        "https://modules.example/main.html",
        "https://modules.example\\@elsewhere/main.mjs",
        "https://modules.example/main.mjs\n",
    ],
)
def test_descriptor_rejects_noncanonical_executable_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        LessonModuleDescriptor(id="test", api_version=1, entry_url=url)


def test_origin_and_api_version_are_explicit(monkeypatch: MonkeyPatch) -> None:
    descriptor = LessonModuleDescriptor(id="test", api_version=1, entry_url="https://modules.example/hash/main.mjs")
    monkeypatch.setattr(settings, "lesson_module_origins", ["https://modules.example"])
    assert checked_descriptor(descriptor) == descriptor
    with pytest.raises(ValueError, match="origin"):
        checked_descriptor(descriptor.copy(update={"entry_url": "https://elsewhere.example/main.mjs"}))
    for version in (True, 1.0, "1", 2):
        with pytest.raises(ValidationError):
            LessonModuleDescriptor.parse_obj({**descriptor.dict(), "api_version": version})
    local = LessonModuleDescriptor(id="local", api_version=1, entry_url="http://127.0.0.1:9123/main.mjs")
    monkeypatch.setattr(settings, "lesson_module_origins", ["http://127.0.0.1:9123"])
    with pytest.raises(ValueError, match="HTTPS"):
        checked_descriptor(local)
    monkeypatch.setattr(settings, "lesson_module_local_development", True)
    assert checked_descriptor(local) == local


async def test_registry_replay_replacement_and_runtime_policy(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "lesson_module_origins", ["https://modules.example"])
    first = LessonModuleDescriptor(id="test", api_version=1, entry_url="https://modules.example/first/main.mjs")
    second = LessonModuleDescriptor(id="test", api_version=1, entry_url="https://modules.example/second/main.mjs")
    async with db_context():
        await register_module(first)
        await register_module(first)
        assert await resolve_module("test") == first
    with pytest.raises(ValueError, match="already registered"):
        async with db_context():
            await register_module(second)
    async with db_context():
        assert await resolve_module("test") == first
        await register_module(second, replace=True)
    async with db_context():
        assert await resolve_module("test") == second
    monkeypatch.setattr(settings, "lesson_module_origins", [])
    async with db_context():
        with pytest.raises(HTTPException) as error:
            await resolve_module("test")
        assert error.value.status_code == 503


def test_registry_migration_preserves_existing_rows() -> None:
    path = Path(__file__).parents[2] / "alembic/versions/2026_09_13_1800-lessonmodules001_registry.py"
    spec = spec_from_file_location("module_migration", path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "learningreviews001"
    engine = create_engine("sqlite://", future=True)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE existing_work (id TEXT, draft TEXT)"))
            connection.execute(text("INSERT INTO existing_work VALUES ('old-id','private draft')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert tuple(connection.execute(text("SELECT * FROM existing_work")).one()) == ("old-id", "private draft")
            assert connection.execute(text("SELECT COUNT(*) FROM skills_lesson_modules")).scalar() == 0
    finally:
        engine.dispose()
