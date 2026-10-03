"""Bounded video reads shared by ordinary and retained-learning delivery."""

import re
from pathlib import Path
from stat import S_ISREG
from typing import AsyncGenerator, Mapping

import anyio
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from api.settings import settings


READ_SIZE = 64 * 1024


async def _chunks(path: Path, start: int, length: int) -> AsyncGenerator[bytes, None]:
    source = await anyio.open_file(path, "rb")
    try:
        await source.seek(start)
        while length > 0:
            data = await source.read(min(READ_SIZE, length))
            if not data:
                break
            length -= len(data)
            yield data
    finally:
        # Disconnect cancellation must still close the owned descriptor.
        with anyio.CancelScope(shield=True):
            await source.aclose()


async def _close_chunks(chunks: AsyncGenerator[bytes, None]) -> None:
    await chunks.aclose()


async def mp4_range_response(path: Path, range_header: str, *, headers: Mapping[str, str] | None = None) -> Response:
    file_stat = await run_in_threadpool(path.stat)
    if not S_ISREG(file_stat.st_mode):
        raise FileNotFoundError(path)
    size = file_stat.st_size
    response_headers = {**(headers or {}), "Accept-Ranges": "bytes"}
    match = re.fullmatch(r"bytes=([0-9]{1,16})-([0-9]{1,16})?", range_header)
    start, end = 0, 0
    if match:
        start = int(match[1])
        end = min(int(match[2]) + 1 if match[2] else start + settings.stream_chunk_size, size)
    if not match or start >= size or end <= start:
        return Response(
            status_code=416, media_type="video/mp4", headers={**response_headers, "Content-Range": f"bytes */{size}"}
        )

    chunks = _chunks(path, start, end - start)
    return StreamingResponse(
        chunks,
        status_code=206,
        media_type="video/mp4",
        headers={
            **response_headers,
            "Content-Range": f"bytes {start}-{end - 1}/{size}",
            "Content-Length": str(end - start),
        },
        # Starlette may stop while the async generator is suspended at yield.
        background=BackgroundTask(_close_chunks, chunks),
    )
