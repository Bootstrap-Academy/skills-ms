"""Explicit limited-service course routes; no ordinary login or publication authority."""

import json
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from httpx import HTTPError
from pydantic import ValidationError

from . import course as courses
from api.redis import redis
from api.schemas.course import Course, Lecture
from api.schemas.user import User
from api.services import purchases
from api.services.courses import COURSES
from api.services.internal import InternalService
from api.settings import settings


router = APIRouter(prefix="/learning")


async def learning_subject(digest: str) -> User:
    """Every admission checks the current coherent backend decision, uncached."""
    try:
        async with InternalService.SHOP.client as client:
            client.event_hooks["response"] = []
            response = await client.post("/claims/learning_authority_digest", json={"hash": digest})
        if response.status_code in (401, 403):
            raise HTTPException(401, "Limited learning authority unavailable")
        if response.status_code != 200:
            raise HTTPException(503, "Current learning authority could not be checked")
        value = response.json()
        if value is None:
            raise HTTPException(401, "Limited learning authority unavailable")
        if (
            value.get("purpose") != "retained_learning"
            or value.get("ordinary_authority") is not False
            or value.get("financial_authority") is not False
            or value.get("admin") is not False
            or value.get("email_verified") is not True
        ):
            raise HTTPException(503, "Invalid scoped authority response")
        return User(id=value["subject"], email_verified=True, admin=False)
    except (HTTPError, ValueError, KeyError, TypeError, ValidationError):
        raise HTTPException(503, "Current learning authority could not be checked") from None


@Depends
async def learning_auth(request: Request) -> User:
    key = request.headers.get("x-learning-key", "")
    if not 43 <= len(key) <= 256:
        raise HTTPException(401, "Limited learning credential required")
    digest = sha256(key.encode()).hexdigest()
    user = await learning_subject(digest)
    # The same local subject lock owns erasure and course delivery. A request
    # waiting behind erasure must not recreate progress/access afterward.
    guard = await purchases.lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This service subject was erased; retained rights remain available")
    current = await learning_subject(digest)
    if current.id != user.id:
        raise HTTPException(503, "Scoped subject changed during admission")
    return current


@Depends
async def learning_course_access(course: Course = courses.get_course, user: User = learning_auth) -> None:
    await courses.has_course_access.dependency(course=course, user=user)


@router.get("/courses")
async def list_courses(user: User = learning_auth) -> Any:
    return await courses.list_courses(None, None, None, None, None, False, user)


@router.get("/course_access")
async def course_access(user: User = learning_auth) -> Any:
    return await courses.get_accessible_courses(user)


@router.get("/courses/{course_id}", dependencies=[learning_course_access])
async def course_details(course: Course = courses.get_course, user: User = learning_auth) -> Any:
    return await courses.get_course_details(course=course, user=user)


@router.post("/courses/{course_id}/watch", dependencies=[learning_course_access])
async def watch(course: Course = courses.get_course, user: User = learning_auth) -> Any:
    return await courses.watch_course(course=course, user=user)


@router.get("/courses/{course_id}/next_unseen", dependencies=[learning_course_access])
async def next_unseen(course: Course = courses.get_course, user: User = learning_auth) -> Any:
    return await courses.next_unseen_lecture(course=course, user=user)


@router.put("/courses/{course_id}/lectures/{lecture_id}/complete", dependencies=[learning_course_access])
async def complete(
    course: Course = courses.get_course, lecture: Lecture = courses.get_lecture, user: User = learning_auth
) -> Any:
    # Preserve actual configured XP and the existing duplicate-completion guard.
    return await courses.complecte_lecture(course=course, lecture=lecture, user=user)


@router.post("/course_access/{course_id}/offer")
async def offer(course: Course = courses.get_course, user: User = learning_auth) -> Any:
    return await purchases.offer(user.id, course)


@router.post("/course_access/{course_id}")
async def buy(data: purchases.Acceptance, course: Course = courses.get_course, user: User = learning_auth) -> Any:
    # The backend submission guard requires a separate exact claimant election.
    # The learning credential and this microservice's transport cannot supply it.
    return await purchases.buy(user.id, course, data)


@router.get("/courses/{course_id}/lectures/{lecture_id}", dependencies=[learning_course_access])
async def lecture_link(
    request: Request,
    course: Course = courses.get_course,
    lecture: Lecture = courses.get_lecture,
    user: User = learning_auth,
) -> Any:
    path = settings.mp4_lectures.joinpath(course.id, lecture.id + ".mp4")
    if lecture.type != "mp4" or not path.is_file():
        raise HTTPException(404, "Lecture unavailable")
    token = token_urlsafe(64)
    name = f"{course.id}_{lecture.id}.mp4"
    await redis.setex(
        f"learning_mp4:{token}:{name}",
        min(settings.stream_token_ttl, 900),
        json.dumps(
            {
                "path": str(path),
                "course": course.id,
                "subject": user.id,
                "authority": sha256(request.headers["x-learning-key"].encode()).hexdigest(),
            }
        ),
    )
    return f"{settings.public_base_url.rstrip('/')}/learning/lectures/{token}/{name}"


@router.get("/lectures/{token}/{file}", include_in_schema=False)
async def stream(token: str, file: str, range: str = Header("bytes=0-", regex=r"^bytes=\d{1,16}-(\d{1,16})?$")) -> Any:
    raw = await redis.get(f"learning_mp4:{token}:{file}")
    if raw is None:
        raise HTTPException(404, "Lecture link unavailable")
    record = json.loads(raw)
    user = await learning_subject(record["authority"])
    if user.id != record["subject"] or record["course"] not in COURSES:
        raise HTTPException(404, "Lecture link unavailable")
    await courses.has_course_access.dependency(course=COURSES[record["course"]], user=user)
    path = Path(record["path"])
    if not path.is_file():
        raise HTTPException(404, "Lecture unavailable")
    start_text, end_text = range.removeprefix("bytes=").split("-")
    start = int(start_text)
    size = path.stat().st_size
    end = min(int(end_text) + 1 if end_text else start + settings.stream_chunk_size, size)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    if start >= size or end <= start:
        return Response(
            status_code=416, media_type="video/mp4", headers={**headers, "Content-Range": f"bytes */{size}"}
        )
    with path.open("rb") as source:
        source.seek(start)
        data = source.read(end - start)
    return Response(
        data,
        status_code=206,
        media_type="video/mp4",
        headers={**headers, "Content-Range": f"bytes {start}-{end - 1}/{size}"},
    )
