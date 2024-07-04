"""Endpoints related to courses and lectures"""

from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Iterable

from fastapi import APIRouter, Depends, Header, Query, Response

from api import models
from api.auth import public_auth, require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import user_responses, verified_responses
from api.exceptions.course import (
    AlreadyCompletedLectureException,
    AlreadyPurchasedCourseException,
    CourseIsFreeException,
    CourseNotFoundException,
    LectureNotFoundException,
    NoCourseAccessException,
    NotEnoughCoinsError,
)
from api.redis import redis
from api.schemas.course import Course, CourseSummary, Lecture, NextUnseenResponse, UserCourse
from api.schemas.user import User
from api.services.auth import get_email
from api.services.courses import COURSES
from api.services.shop import has_premium, spend_coins
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached
from api.utils.docs import responses
from api.utils.email import BOUGHT_COURSE


router = APIRouter()


@Depends
async def get_course(course_id: str) -> Course:
    if course_id not in COURSES:
        raise CourseNotFoundException
    return COURSES[course_id]


@Depends
async def get_lecture(lecture_id: str, course: Course = get_course) -> Lecture:
    for section in course.sections:
        for lecture in section.lectures:
            if lecture.id == lecture_id:
                return lecture
    raise LectureNotFoundException


@Depends
async def has_course_access(course: Course = get_course, user: User = user_auth) -> None:
    """Check if the user has access to the course"""

    if course.free or user.admin:
        return

    if course.id in await get_owned_courses(user.id):
        return

    if await has_premium(user.id):
        return

    raise NoCourseAccessException


@redis_cached("course_access", "user_id")
async def get_owned_courses(user_id: str) -> set[str]:
    return {ca.course_id async for ca in await db.stream(filter_by(models.CourseAccess, user_id=user_id))} | {
        lw.course_id async for lw in await db.stream(filter_by(models.LastWatch, user_id=user_id))
    }


@router.get("/courses", responses=responses(list[CourseSummary]))
async def list_courses(
    search_term: str | None = Query(None, max_length=256, description="A search term to filter courses by"),
    language: str | None = Query(None, max_length=256, description="The language to search for"),
    author: str | None = Query(None, max_length=256, description="The author to search for"),
    free: bool | None = Query(None, description="Whether to search for free courses"),
    owned: bool | None = Query(None, description="Whether to search for courses the user owns"),
    recent_first: bool = Query(False, description="Whether to return the most recently watched courses first"),
    user: User | None = public_auth,
) -> Any:
    """Return a list of all available courses."""

    out: Iterable[Course] = iter(COURSES.values())

    if search_term:
        out = (course for course in out if search_term.lower() in course.title.lower())
    if language:
        out = (course for course in out if course.language is not None and language.lower() in course.language.lower())
    if author:
        out = [
            course
            for course in out
            if course.authors is not None and any(author.lower() == a["name"].lower() for a in course.authors)
        ]
    if free is not None:
        out = (course for course in out if course.free == free)
    if owned is not None:
        courses = set()
        if user:
            courses |= await get_owned_courses(user.id)

        relevant = courses if owned else set(COURSES) - courses
        out = (course for course in out if course.id in relevant)

    if recent_first and user:
        last_watches = {
            lw.course_id: lw.timestamp.timestamp()
            async for lw in await db.stream(filter_by(models.LastWatch, user_id=user.id))
        }
        out = sorted(out, key=lambda c: last_watches.get(c.id, 0), reverse=True)

    completed_lectures: dict[str, set[str]] | None = None
    if user:
        completed_lectures = {}
        async for lecture in await db.stream(filter_by(models.LectureProgress, user_id=user.id)):
            completed_lectures.setdefault(lecture.course_id, set()).add(lecture.lecture_id)

    return [
        course.summary(None if completed_lectures is None else completed_lectures.get(course.id, set()))
        for course in out
    ]


@router.get("/courses/{course_id}/summary", responses=responses(CourseSummary, CourseNotFoundException))
async def get_course_summary(course: Course = get_course, user: User | None = public_auth) -> Any:
    """Return a summary of the course."""

    return course.summary(None if user is None else await models.LectureProgress.get_completed(user.id, course.id))


@router.post(
    "/courses/{course_id}/watch",
    dependencies=[require_verified_email, has_course_access],
    responses=user_responses(bool, CourseNotFoundException, NoCourseAccessException),
)
async def watch_course(course: Course = get_course, user: User = user_auth) -> Any:
    """Mark the course as watched for the user."""

    await models.LastWatch.update(user.id, course.id)
    return True


@router.get(
    "/courses/{course_id}",
    dependencies=[require_verified_email, has_course_access],
    responses=verified_responses(UserCourse, NoCourseAccessException, CourseNotFoundException),
)
@redis_cached("lecture_progress", "course", "user")
async def get_course_details(course: Course = get_course, user: User = user_auth) -> Any:
    """
    Return details about a specific course.

    For premium courses the user must have access to the course.

    *Requirements:* **VERIFIED**
    """

    return course.to_user_course(await models.LectureProgress.get_completed(user.id, course.id))


@router.get(
    "/courses/{course_id}/lectures/{lecture_id}",
    dependencies=[require_verified_email, has_course_access],
    responses=verified_responses(str, NoCourseAccessException, CourseNotFoundException, LectureNotFoundException),
)
async def get_mp4_lecture_link(course: Course = get_course, lecture: Lecture = get_lecture) -> Any:
    """
    Return the download link of an mp4 lecture.

    *Requirements:* **VERIFIED**
    """

    if lecture.type != "mp4":
        raise LectureNotFoundException

    path = settings.mp4_lectures.joinpath(course.id, lecture.id + ".mp4")
    if not path.is_file():
        raise LectureNotFoundException

    token = token_urlsafe(64)
    name = f"{course.id}_{lecture.id}.mp4"
    await redis.setex(f"mp4_lecture:{token}:{name}", settings.stream_token_ttl, str(path))

    return f"{settings.public_base_url.rstrip('/')}/lectures/{token}/{name}"


@router.get("/lectures/{token}/{file}", include_in_schema=False)
async def download_mp4_lecture(
    token: str, file: str, range: str = Header("bytes=0-", regex=r"^bytes=\d{1,16}-(\d{1,16})?$")
) -> Any:
    path = await redis.get(f"mp4_lecture:{token}:{file}")
    if not path:
        raise LectureNotFoundException

    path = Path(path)
    _start, _end = range.removeprefix("bytes=").split("-")
    start = int(_start)
    end = max(start, int(_end) + 1) if _end else start + settings.stream_chunk_size
    filesize = path.stat().st_size
    end = min(end, filesize)
    with open(path, "rb") as video:
        video.seek(start)
        data = video.read(end - start)
        headers = {"Content-Range": f"bytes {start}-{end - 1}/{filesize}", "Accept-Ranges": "bytes"}
        return Response(data, status_code=206, headers=headers, media_type="video/mp4")


@router.get(
    "/courses/{course_id}/next_unseen",
    dependencies=[require_verified_email, has_course_access],
    responses=responses(NextUnseenResponse, CourseNotFoundException, NoCourseAccessException),
)
async def next_unseen_lecture(course: Course = get_course, user: User = user_auth) -> Any:
    already_watched = await models.LectureProgress.get_completed(user_id=user.id, course_id=course.id)
    for section in course.sections:
        for lecture in section.lectures:
            if lecture.id not in already_watched:
                return NextUnseenResponse(section=section, lecture=lecture)

    section = course.sections[0]
    lecture = section.lectures[0]
    return NextUnseenResponse(section=section, lecture=lecture)


@router.put(
    "/courses/{course_id}/lectures/{lecture_id}/complete",
    dependencies=[require_verified_email, has_course_access],
    responses=verified_responses(
        bool,
        AlreadyCompletedLectureException,
        NoCourseAccessException,
        CourseNotFoundException,
        LectureNotFoundException,
    ),
)
async def complecte_lecture(
    *, course: Course = get_course, user: User = user_auth, lecture: Lecture = get_lecture
) -> Any:
    """
    Mark a lecture as completed.

    *Requirements:* **VERIFIED**
    """

    if await models.LectureProgress.is_completed(user.id, course.id, lecture.id):
        raise AlreadyCompletedLectureException

    await models.LectureProgress.set_completed(user.id, course.id, lecture.id)
    async for skill_course in await db.stream(filter_by(models.SkillCourse, course_id=course.id)):
        await models.XP.add_xp(user.id, skill_course.skill_id, settings.lecture_xp)

    await clear_cache("xp")
    await clear_cache("lecture_progress")

    return True


@router.get("/course_access", dependencies=[require_verified_email], responses=verified_responses(list[CourseSummary]))
async def get_accessible_courses(user: User = user_auth) -> Any:
    """
    Return a list of all courses the user has access to.

    *Requirements:* **VERIFIED**
    """

    completed_lectures: dict[str, set[str]] = {}
    async for lecture in await db.stream(filter_by(models.LectureProgress, user_id=user.id)):
        completed_lectures.setdefault(lecture.course_id, set()).add(lecture.lecture_id)

    course_ids = {k for k, v in COURSES.items() if v.free or user.admin}
    course_ids |= (await get_owned_courses(user.id)) & set(COURSES)
    return [COURSES[course_id].summary(completed_lectures.get(course_id, set())) for course_id in course_ids]


@router.post(
    "/course_access/{course_id}",
    dependencies=[require_verified_email],
    responses=verified_responses(bool, CourseIsFreeException, AlreadyPurchasedCourseException, NotEnoughCoinsError),
)
async def buy_course(user: User = user_auth, course: Course = get_course) -> Any:
    """
    Buy access to a course for a user.

    *Requirements:* **VERIFIED**
    """

    if course.free:
        raise CourseIsFreeException

    if await db.exists(filter_by(models.CourseAccess, user_id=user.id, course_id=course.id)):
        raise AlreadyPurchasedCourseException

    if not await spend_coins(user.id, course.price, f"Course '{course.title}'"):
        raise NotEnoughCoinsError

    await models.CourseAccess.create(user.id, course.id)
    if email := await get_email(user.id):
        await BOUGHT_COURSE.send(email, title=course.title)

    await clear_cache("course_access")

    return True
