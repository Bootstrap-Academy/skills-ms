"""Both video routes must stream bounded reads without blocking other requests."""

import asyncio
import builtins
import json
from pathlib import Path
from threading import Event, get_ident
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pytest import MonkeyPatch

from api.endpoints import course, learning
from api.redis import redis
from api.schemas.user import User
from api.settings import settings


@pytest.fixture(autouse=True)
def database() -> None:
    """The isolated routers need no database or live service."""


@pytest.fixture(params=["ordinary", "learning"])
async def video_fixture(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    path = tmp_path / "video.mp4"
    path.write_bytes(bytes(range(256)))
    scoped = request.param == "learning"
    prefix = "learning_mp4" if scoped else "mp4_lecture"
    url = ("/learning" if scoped else "") + "/lectures/synthetic-token/course_lecture.mp4"
    record = {"path": str(path), "subject": "synthetic-user", "course": "course", "authority": "synthetic-digest"}

    async def lookup(key: str) -> str | None:
        if key != f"{prefix}:synthetic-token:course_lecture.mp4":
            return None
        return json.dumps(record) if scoped else str(path)

    lookup_mock = AsyncMock(side_effect=lookup)
    authority = AsyncMock(return_value=User(id="synthetic-user", email_verified=True, admin=False))
    admission = AsyncMock()
    monkeypatch.setattr(redis, "get", lookup_mock)
    monkeypatch.setattr(learning, "learning_subject", authority)
    monkeypatch.setattr(learning, "COURSES", {"course": object()})
    monkeypatch.setattr(course.has_course_access, "dependency", admission)
    monkeypatch.setattr(settings, "stream_chunk_size", 4 * 1024 * 1024)
    app = FastAPI()
    app.include_router(learning.router if scoped else course.router)

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"responsive": True}

    async with AsyncClient(app=app, base_url="http://video.synthetic") as client:
        yield {"path": path, "url": url, "client": client, "app": app, "authority": authority, "admission": admission}


@pytest.mark.parametrize(
    "range_header", ["bytes=256-", "bytes=1-0", "bytes=0-1,5-8", "bytes=-32", "bytes=x-3", "bytes=10000000000000000-"]
)
async def test_invalid_ranges_return_416(video_fixture: dict[str, Any], range_header: str) -> None:
    response = await video_fixture["client"].get(video_fixture["url"], headers={"Range": range_header})
    assert response.status_code == 416
    assert response.content == b""
    assert response.headers["Content-Range"] == "bytes */256"
    assert response.headers["Content-Length"] == "0"
    assert response.headers["Accept-Ranges"] == "bytes"


@pytest.mark.parametrize("change", ["token", "filename", "missing_file", "empty_file"])
async def test_token_filename_and_file_boundaries(video_fixture: dict[str, Any], change: str) -> None:
    f = video_fixture
    url = f["url"]
    if change == "token":
        url = url.replace("synthetic-token", "expired-token")
    elif change == "filename":
        url = url.replace("course_lecture", "other-course_lecture")
    elif change == "missing_file":
        f["path"].unlink()
    else:
        f["path"].write_bytes(b"")
    response = await f["client"].get(url, headers={"Range": "bytes=0-9999999999999999"})
    assert response.status_code == (416 if change == "empty_file" else 404)
    if change in ("token", "filename"):
        f["authority"].assert_not_awaited()
        f["admission"].assert_not_awaited()
    if change == "empty_file":
        assert response.headers["Content-Range"] == "bytes */0"


async def test_default_and_seek_preserve_bytes_and_headers(video_fixture: dict[str, Any]) -> None:
    f = video_fixture
    for headers, start, end in [({}, 0, 256), ({"Range": "bytes=64-95"}, 64, 96)]:
        response = await f["client"].get(f["url"], headers=headers)
        assert response.status_code == 206
        assert response.content == bytes(range(256))[start:end]
        assert response.headers["Content-Range"] == f"bytes {start}-{end - 1}/256"
        assert response.headers["Content-Length"] == str(end - start)
        assert response.headers["Content-Type"] == "video/mp4"


async def test_large_explicit_range_uses_bounded_worker_reads_and_keeps_other_requests_responsive(
    video_fixture: dict[str, Any], monkeypatch: MonkeyPatch
) -> None:
    f = video_fixture
    data = bytes(range(256)) * (32 * 1024) + b"synthetic trailer"
    f["path"].write_bytes(data)
    started, release = Event(), Event()
    loop_thread = get_ident()
    reads: list[tuple[int, int]] = []
    opened: list[Any] = []
    original_open, original_path_open = builtins.open, Path.open

    def tracked_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        raw = original_open(file, *args, **kwargs)
        if file not in (f["path"], str(f["path"])):
            return raw
        opened.append(raw)
        wrapped = MagicMock(wraps=raw)

        def read(size: int = -1) -> bytes:
            reads.append((get_ident(), size))
            if len(reads) == 1:
                started.set()
                assert release.wait(3), "The first read must run off the event loop"
            return bytes(raw.read(size))

        wrapped.read.side_effect = read
        wrapped.__enter__.return_value = wrapped
        wrapped.__exit__.side_effect = lambda *_: raw.close()
        return wrapped

    def path_open(path: Path, *args: Any, **kwargs: Any) -> Any:
        return tracked_open(path, *args, **kwargs) if path == f["path"] else original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(Path, "open", path_open)
    download = asyncio.create_task(f["client"].get(f["url"], headers={"Range": "bytes=0-9999999999999999"}))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert not download.done()
        probe = await asyncio.wait_for(f["client"].get("/probe"), timeout=1)
        assert probe.status_code == 200
        assert probe.json() == {"responsive": True}
        assert not download.done(), "The probe must complete while the file read is still blocked"
        release.set()
        response = await asyncio.wait_for(download, timeout=10)
        assert response.status_code == 206
        assert response.content == data
        assert response.headers["Content-Range"] == f"bytes 0-{len(data) - 1}/{len(data)}"
        assert response.headers["Content-Length"] == str(len(data))
        assert all(thread != loop_thread and 0 < size <= settings.stream_chunk_size for thread, size in reads)
        assert len(reads) > 1
        assert opened and all(file.closed for file in opened)
        implicit = await f["client"].get(f["url"], headers={"Range": "bytes=0-"})
        assert implicit.status_code == 206
        assert implicit.content == data[: settings.stream_chunk_size]
        assert implicit.headers["Content-Length"] == str(settings.stream_chunk_size)
    finally:
        release.set()
        await asyncio.gather(download, return_exceptions=True)


async def test_disconnect_closes_the_file_with_an_unfinished_range(
    video_fixture: dict[str, Any], monkeypatch: MonkeyPatch
) -> None:
    f = video_fixture
    f["path"].write_bytes(b"x" * (256 * 1024))
    opened: list[Any] = []
    original_open = anyio.open_file

    async def track_open(*args: Any, **kwargs: Any) -> Any:
        file = await original_open(*args, **kwargs)
        opened.append(file.wrapped)
        return file

    monkeypatch.setattr(anyio, "open_file", track_open)
    disconnected = asyncio.Event()
    bodies: list[bytes] = []

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body" and message["body"]:
            bodies.append(message["body"])
            disconnected.set()
            await asyncio.sleep(0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": f["url"],
        "raw_path": f["url"].encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"range", b"bytes=0-9999999999999999")],
    }
    await asyncio.wait_for(f["app"](scope, receive, send), timeout=3)
    assert bodies and sum(map(len, bodies)) < f["path"].stat().st_size
    assert opened and all(file.closed for file in opened)
