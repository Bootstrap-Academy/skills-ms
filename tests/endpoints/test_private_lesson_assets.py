"""Real lesson/room routing and SQL admission, with an explicitly isolated Redis clock."""

import hashlib
import json
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock
from urllib.parse import quote, urlsplit

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from pytest import MonkeyPatch

from api import models
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints.curriculum import router as curriculum_router
from api.endpoints.lesson_assets import router as asset_router
from api.endpoints.rooms import router as rooms_router
from api.schemas.lesson_module import LessonModuleDescriptor
from api.schemas.rooms import Catalogue, CatalogueUnit
from api.schemas.user import User
from api.services import private_lesson_modules as private
from api.services import rooms
from api.services.courses import COURSES
from api.services.lesson_modules import register_module
from api.settings import settings
from tests.endpoints.test_curriculum import composed, course_definition
from tests.endpoints.test_rooms import content, payload


content = content


class IsolatedRedis:
    def __init__(self) -> None:
        self.now = 0
        self.values: dict[str, tuple[int, str]] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = (self.now + ttl, value)

    async def get(self, key: str) -> str | None:
        expiry, value = self.values.get(key, (0, ""))
        return value if expiry > self.now else None


def package(tmp_path: Path, monkeypatch: MonkeyPatch, revision: int = 1) -> tuple[LessonModuleDescriptor, Path]:
    definition = {"id": "private-fixture", "api_version": 1, "entry": "entry.mjs"}
    files = {
        "entry.mjs": f"export const apiVersion=1; // fixture {revision}".encode(),
        "assets/übung $+.svg": b"<svg/>",
    }
    inventory = [
        {"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    ]
    artifact = hashlib.sha256(
        json.dumps({"definition": definition, "files": inventory}, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    descriptor = LessonModuleDescriptor(
        id="private-fixture",
        api_version=1,
        entry_url=f"https://api.example/private-lesson-modules/{artifact}/entry.mjs",
    )
    root = tmp_path / "private-modules"
    directory = root / artifact
    directory.mkdir(parents=True)
    for name, data in files.items():
        target = directory / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (directory / "manifest.json").write_text(
        json.dumps({"artifact_sha256": artifact, "definition": definition, "files": inventory})
    )
    (directory / "module.json").write_text(descriptor.json())
    monkeypatch.setattr(settings, "private_lesson_modules_root", root)
    monkeypatch.setattr(settings, "public_base_url", "https://api.example/skills")
    monkeypatch.setattr(settings, "lesson_module_origins", ["https://api.example"])
    return descriptor, directory


@pytest.fixture
async def setup_private(
    content: Catalogue, tmp_path: Path, monkeypatch: MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, IsolatedRedis, Path]]:
    descriptor, directory = package(tmp_path, monkeypatch)
    content.units[0] = CatalogueUnit.parse_obj(
        {**content.units[0].dict(), "room": "custom", "module_id": descriptor.id}
    )
    course = course_definition(price=100, curriculum=composed("intro"))
    monkeypatch.setitem(COURSES, course.id, course)
    monkeypatch.setattr(rooms, "has_premium", AsyncMock(return_value=False))
    monkeypatch.setattr("api.endpoints.course.has_premium", AsyncMock(return_value=False))
    cache = IsolatedRedis()
    monkeypatch.setattr(private, "redis", cache)
    async with db_context():
        await register_module(descriptor)
        await db.add(models.CourseAccess(user_id="owner", course_id=course.id))

    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        uid = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if uid not in ("owner", "unpaid", "unverified"):
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=uid, email_verified=uid != "unverified", admin=False)

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = identity
    for router in (curriculum_router, rooms_router, asset_router):
        app.include_router(router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(app=app, base_url="https://api.example") as client:
        yield client, cache, directory


async def test_admission_expiry_renewal_and_mutation_keep_module_identity(
    setup_private: tuple[httpx.AsyncClient, IsolatedRedis, Path]
) -> None:
    client, cache, directory = setup_private
    lesson = "/courses/composed/lessons/combined"
    for user, status in ((None, 401), ("unverified", 403), ("unpaid", 403)):
        response = await client.get(lesson, headers={"Authorization": f"Bearer {user}"} if user else {})
        assert response.status_code == status
        assert not cache.values
    response = await client.get(lesson, headers={"Authorization": "Bearer owner"})
    assert response.status_code == 200
    module = response.json()["activities"][0]["module"]
    path = urlsplit(module["entry_url"]).path.removeprefix("/skills")
    assert "/lesson-assets/" in path and "/private-lesson-modules/" not in path
    assert (
        module
        == (await client.get(lesson, headers={"Authorization": "Bearer owner"})).json()["activities"][0]["module"]
    )
    for method in (client.get, client.head):
        asset = await method(path)
        assert asset.status_code == 200
        assert asset.headers["x-accel-redirect"] == f"/_private-lesson-modules/{directory.name}/entry.mjs"
        assert asset.headers["content-type"] == "application/javascript"
        assert asset.headers["cache-control"] == "private, no-store"
        assert asset.headers["referrer-policy"] == "no-referrer"
    cache.now += settings.private_lesson_module_grant_ttl + 1
    assert (await client.get(path)).status_code == 404
    # Expired/cleared Redis does not change the component identity during the
    # checkpoint PUT preceding a real assessment submission.
    cache.values.clear()
    saved = await client.put(
        "/rooms/intro/state?course=composed",
        json=payload(state={"draft": "kept"}),
        headers={"Authorization": "Bearer owner"},
    )
    assert saved.status_code == 200 and saved.json()["unit"]["module"] == module
    assert saved.json()["progress"]["state"] == {"draft": "kept"}
    assert (await client.get(path)).status_code == 200
    async with db_context():
        assert await db.all(filter_by(models.XP, user_id="owner")) == []
        rows = await db.all(filter_by(models.RoomState, user_id="owner"))
        assert len(rows) == 1 and rows[0].status == "in_progress" and rows[0].result is None


async def test_asset_inventory_traversal_symlinks_and_registry_withdrawal(
    setup_private: tuple[httpx.AsyncClient, IsolatedRedis, Path], monkeypatch: MonkeyPatch
) -> None:
    client, _, directory = setup_private
    opened = await client.get("/courses/composed/lessons/combined", headers={"Authorization": "Bearer owner"})
    path = urlsplit(opened.json()["activities"][0]["module"]["entry_url"]).path.removeprefix("/skills")
    base = path.rsplit("/", 1)[0]
    asset = await client.get(base + "/" + quote("assets/übung $+.svg", safe="/"))
    assert asset.status_code == 200 and asset.headers["content-type"] == "image/svg+xml"
    assert asset.headers["x-accel-redirect"].endswith("assets/%C3%BCbung%20%24%2B.svg")
    (directory / "unlisted.mjs").write_text("private and unlisted")
    for name in ("unlisted.mjs", "manifest.json", "module.json", "%2e%2e/entry.mjs", ".secret", "assets%5coutside"):
        denied = await client.get(base + "/" + name)
        assert denied.status_code == 404 and "x-accel-redirect" not in denied.headers
    wrong = base.replace(directory.name, "0" * 64) + "/entry.mjs"
    assert (await client.get(wrong)).status_code == 404
    original = (directory / "entry.mjs").read_bytes()
    (directory / "entry.mjs").write_bytes(b"tampered")
    assert (await client.get(path)).status_code == 404
    (directory / "entry.mjs").unlink()
    outside = directory.parent / "outside.mjs"
    outside.write_bytes(original)
    (directory / "entry.mjs").symlink_to(outside)
    assert (await client.get(path)).status_code == 404
    (directory / "entry.mjs").unlink()
    (directory / "entry.mjs").write_bytes(original)
    root_alias = directory.parent.parent / "root-symlink"
    root_alias.symlink_to(directory.parent, target_is_directory=True)
    monkeypatch.setattr(settings, "private_lesson_modules_root", root_alias)
    assert (await client.get(path)).status_code == 404
    monkeypatch.setattr(settings, "private_lesson_modules_root", directory.parent)
    actual = directory / "real-assets"
    (directory / "assets").rename(actual)
    (directory / "assets").symlink_to(actual, target_is_directory=True)
    assert (await client.get(base + "/" + quote("assets/übung $+.svg", safe="/"))).status_code == 404
    async with db_context():
        row = await db.get(models.LessonModule, id="private-fixture")
        assert row is not None
        await db.session.delete(row)
    assert (await client.get(path)).status_code == 404


async def test_registry_replacement_keeps_old_grants_only_until_their_existing_expiry(
    setup_private: tuple[httpx.AsyncClient, IsolatedRedis, Path], monkeypatch: MonkeyPatch
) -> None:
    client, cache, first_directory = setup_private
    lesson = "/courses/composed/lessons/combined"
    opened = await client.get(lesson, headers={"Authorization": "Bearer owner"})
    first = opened.json()["activities"][0]["module"]["entry_url"]
    old_path = urlsplit(first).path.removeprefix("/skills")
    replacement, _ = package(first_directory.parent.parent, monkeypatch, revision=2)
    async with db_context():
        await register_module(replacement, replace=True)
    assert (await client.get(old_path)).status_code == 200
    cache.now += 600
    second = (await client.get(lesson, headers={"Authorization": "Bearer owner"})).json()["activities"][0]["module"][
        "entry_url"
    ]
    assert second != first
    new_path = urlsplit(second).path.removeprefix("/skills")
    assert (await client.get(new_path)).status_code == 200
    cache.now = settings.private_lesson_module_grant_ttl + 1
    assert (await client.get(old_path)).status_code == 404
    assert (await client.get(new_path)).status_code == 200
