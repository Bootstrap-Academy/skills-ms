"""Endpoints related to courses and lectures"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, Iterable

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse
from starlette import status

from api import models
from api.auth import public_auth, require_verified_email, user_auth
from api.database import db, filter_by, select
from api.exceptions.auth import user_responses, verified_responses
from api.exceptions.course import (
    AlreadyCompletedLectureException,
    AlreadyPurchasedCourseException,
    CourseIsFreeException,
    CourseNotFoundException,
    LectureNotFoundException,
    NextLabNotFoundException,
    NextLectureNotFoundException,
    NextTaskNotFoundException,
    NoCourseAccessException,
    NotEnoughCoinsError,
)
from api.redis import redis
from api.schemas.course import (
    Course,
    CourseReference,
    CourseSummary,
    Lecture,
    NextLabRecommendation,
    NextLectureRecommendation,
    NextTaskRecommendation,
    NextUnseenResponse,
    Section,
    SectionReference,
    TaskPointer,
    UserCourse,
)
from api.schemas.user import User
from api.services.auth import get_email
from api.services.challenges import (
    SubtaskRecommendation,
    get_unsolved_labs_for_lecture,
    get_unsolved_quizzes_for_lecture,
)
from api.services.courses import COURSES
from api.services.shop import has_premium, spend_coins
from api.settings import settings
from api.utils.cache import clear_cache, redis_cached
from api.utils.docs import responses
from api.utils.email import BOUGHT_COURSE


router = APIRouter()


@dataclass
class CourseProgress:
    completed_lectures: set[str]
    latest_completed_at: datetime | None = None


def _normalize_timestamp(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _course_completed(course: Course, completed_lectures: set[str]) -> bool:
    total_lectures = sum(len(section.lectures) for section in course.sections)
    if total_lectures == 0:
        return True
    return len(completed_lectures) >= total_lectures


async def _collect_course_progress(user_id: str) -> dict[str, CourseProgress]:
    progress: dict[str, CourseProgress] = {}
    async for lecture in await db.stream(filter_by(models.LectureProgress, user_id=user_id)):
        entry = progress.setdefault(lecture.course_id, CourseProgress(completed_lectures=set()))
        entry.completed_lectures.add(lecture.lecture_id)
        if lecture.completed and (entry.latest_completed_at is None or lecture.completed > entry.latest_completed_at):
            entry.latest_completed_at = lecture.completed

    async for last_watch in await db.stream(filter_by(models.LastWatch, user_id=user_id)):
        entry = progress.setdefault(last_watch.course_id, CourseProgress(completed_lectures=set()))
        if entry.latest_completed_at is None or last_watch.timestamp > entry.latest_completed_at:
            entry.latest_completed_at = last_watch.timestamp

    return progress


async def _resolve_current_course(user_id: str) -> tuple[Course, set[str]] | None:
    progress = await _collect_course_progress(user_id)
    candidate: tuple[Course, set[str], datetime] | None = None

    for course_id, course_progress in progress.items():
        course = COURSES.get(course_id)
        if course is None:
            continue

        if _course_completed(course, course_progress.completed_lectures):
            continue

        latest = _normalize_timestamp(course_progress.latest_completed_at)
        if candidate is None or latest > candidate[2]:
            candidate = (course, course_progress.completed_lectures, latest)

    if candidate is None:
        return None

    course, completed, _timestamp = candidate
    return course, completed


async def _resolve_next_course(user_id: str) -> tuple[Course, set[str]] | None:
    progress = await _collect_course_progress(user_id)
    bookmarks = await db.all(filter_by(models.SubSkillBookmark, user_id=user_id))
    bookmarked_skill_ids = {bookmark.sub_skill_id for bookmark in bookmarks}

    candidates: list[tuple[Course, set[str]]] = []
    seen: set[str] = set()

    def append_course(course_id: str) -> None:
        if course_id in seen:
            return
        course = COURSES.get(course_id)
        if course is None:
            return
        entry = progress.get(course_id)
        completed = set() if entry is None else entry.completed_lectures
        if entry and _course_completed(course, completed):
            return
        seen.add(course_id)
        candidates.append((course, completed))

    if bookmarked_skill_ids:
        query = select(models.SkillCourse).where(models.SkillCourse.skill_id.in_(bookmarked_skill_ids))
        async for mapping in await db.stream(query):
            append_course(mapping.course_id)

    if not candidates:
        for course_id in COURSES:
            append_course(course_id)

    if not candidates:
        return None

    return candidates[0]


async def _resolve_course_for_recommendations(user_id: str) -> tuple[Course, set[str]] | None:
    """Return the course context used for lecture/task recommendations."""

    if current := await _resolve_current_course(user_id):
        return current
    if next_course := await _resolve_next_course(user_id):
        return next_course

    # Fallback: return the most recently progressed course even if fully completed.
    progress = await _collect_course_progress(user_id)
    candidate: tuple[Course, set[str], datetime] | None = None
    for course_id, course_progress in progress.items():
        course = COURSES.get(course_id)
        if course is None:
            continue
        latest = _normalize_timestamp(course_progress.latest_completed_at)
        if candidate is None or latest > candidate[2]:
            candidate = (course, course_progress.completed_lectures, latest)

    if candidate:
        course, completed, _ = candidate
        return course, completed

    return None


def _course_reference(course: Course) -> CourseReference:
    return CourseReference(id=course.id, title=course.title, image=course.image)


def _section_reference(section: Section) -> SectionReference:
    return SectionReference(id=section.id, title=section.title)


def _iter_course_lectures(course: Course) -> list[tuple[Section, Lecture]]:
    return [(section, lecture) for section in course.sections for lecture in section.lectures]


def _find_next_unseen_lecture(course: Course, completed: set[str]) -> tuple[Section, Lecture] | None:
    for section, lecture in _iter_course_lectures(course):
        if lecture.id not in completed:
            return section, lecture
    return None


async def _get_completed_lecture_ids(user_id: str, course_id: str) -> list[str]:
    records = [
        record
        async for record in await db.stream(filter_by(models.LectureProgress, user_id=user_id, course_id=course_id))
    ]
    records.sort(key=lambda record: _normalize_timestamp(record.completed), reverse=True)
    return [record.lecture_id for record in records]


def _build_task_recommendation(
    course: Course, section: Section, lecture: Lecture, subtask: SubtaskRecommendation
) -> NextTaskRecommendation:
    return NextTaskRecommendation(
        course=_course_reference(course),
        section=_section_reference(section),
        lecture=lecture,
        task=TaskPointer(id=subtask.task_id, subtask_id=subtask.subtask_id, subtask_type=subtask.subtask_type),
    )


def _build_lab_recommendation(
    course: Course, section: Section, lecture: Lecture, subtask: SubtaskRecommendation
) -> NextLabRecommendation:
    return NextLabRecommendation(
        course=_course_reference(course),
        section=_section_reference(section),
        lecture=lecture,
        task=TaskPointer(id=subtask.task_id, subtask_id=subtask.subtask_id, subtask_type=subtask.subtask_type),
    )


async def _resolve_next_lecture_recommendation(user_id: str) -> NextLectureRecommendation | None:
    context = await _resolve_course_for_recommendations(user_id)
    if context is None:
        return None

    course, completed = context
    next_pair = _find_next_unseen_lecture(course, completed)
    if next_pair is None:
        return None

    section, lecture = next_pair
    return NextLectureRecommendation(
        course=_course_reference(course), section=_section_reference(section), lecture=lecture
    )


def _find_lecture_in_course(course: Course, lecture_id: str) -> tuple[Section, Lecture] | None:
    for section in course.sections:
        for lecture in section.lectures:
            if lecture.id == lecture_id:
                return section, lecture
    return None


async def _resolve_next_task_recommendation(
    user_id: str, *, course_id: str | None = None, lecture_id: str | None = None
) -> NextTaskRecommendation | None:
    if course_id and lecture_id:
        course = COURSES.get(course_id)
        if course is None:
            return None

        section_lecture = _find_lecture_in_course(course, lecture_id)
        if section_lecture is None:
            return None

        section, lecture = section_lecture
        quizzes = await get_unsolved_quizzes_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture.id)
        if not quizzes:
            return None

        return _build_task_recommendation(course, section, lecture, quizzes[0])

    context = await _resolve_course_for_recommendations(user_id)
    if context is None:
        return None

    course, completed = context
    lecture_lookup: dict[str, tuple[Section, Lecture]] = {
        lecture.id: (section, lecture) for section, lecture in _iter_course_lectures(course)
    }

    for lecture_id in await _get_completed_lecture_ids(user_id, course.id):
        section_lecture = lecture_lookup.get(lecture_id)
        if section_lecture is None:
            continue

        section, lecture = section_lecture
        quizzes = await get_unsolved_quizzes_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture_id)
        if quizzes:
            return _build_task_recommendation(course, section, lecture, quizzes[0])

    next_pair = _find_next_unseen_lecture(course, completed)
    if next_pair is None:
        return None

    section, lecture = next_pair
    quizzes = await get_unsolved_quizzes_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture.id)
    if not quizzes:
        return None

    return _build_task_recommendation(course, section, lecture, quizzes[0])


async def _resolve_next_lab_recommendation(
    user_id: str, *, course_id: str | None = None, lecture_id: str | None = None
) -> NextLabRecommendation | None:
    if course_id and lecture_id:
        course = COURSES.get(course_id)
        if course is None:
            return None

        section_lecture = _find_lecture_in_course(course, lecture_id)
        if section_lecture is None:
            return None

        section, lecture = section_lecture
        labs = await get_unsolved_labs_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture.id)
        if not labs:
            return None

        return _build_lab_recommendation(course, section, lecture, labs[0])

    context = await _resolve_course_for_recommendations(user_id)
    if context is None:
        return None

    course, completed = context
    lecture_lookup: dict[str, tuple[Section, Lecture]] = {
        lecture.id: (section, lecture) for section, lecture in _iter_course_lectures(course)
    }

    for lecture_id in await _get_completed_lecture_ids(user_id, course.id):
        section_lecture = lecture_lookup.get(lecture_id)
        if section_lecture is None:
            continue

        section, lecture = section_lecture
        labs = await get_unsolved_labs_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture_id)
        if labs:
            return _build_lab_recommendation(course, section, lecture, labs[0])

    next_pair = _find_next_unseen_lecture(course, completed)
    if next_pair is None:
        return None

    section, lecture = next_pair
    labs = await get_unsolved_labs_for_lecture(user_id=user_id, course_id=course.id, lecture_id=lecture.id)
    if not labs:
        return None

    return _build_lab_recommendation(course, section, lecture, labs[0])


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


@router.get(
    "/courses/current",
    dependencies=[require_verified_email],
    responses=verified_responses(CourseSummary, CourseNotFoundException),
)
async def get_current_course(user: User = user_auth) -> Any:
    """Return the most recently progressed but unfinished course for the user."""

    current = await _resolve_current_course(user.id)
    if current is None:
        fallback = await _resolve_next_course(user.id)
        if fallback is None:
            raise CourseNotFoundException
        course, completed = fallback
        return course.summary(completed)

    course, completed = current
    return course.summary(completed)


@router.get(
    "/courses/next",
    dependencies=[require_verified_email],
    responses=verified_responses(CourseSummary, CourseNotFoundException),
)
async def get_next_course(user: User = user_auth) -> Any:
    """Return a recommended next course (bookmarks preferred, otherwise random)."""

    course = await _resolve_next_course(user.id)
    if course is None:
        raise CourseNotFoundException
    course_obj, completed = course
    return course_obj.summary(completed)


@router.get(
    "/courses/next/lecture",
    dependencies=[require_verified_email],
    responses=verified_responses(NextLectureRecommendation, NextLectureNotFoundException),
)
async def get_next_lecture(user: User = user_auth) -> Any:
    """Return the next lecture recommendation for the user."""

    recommendation = await _resolve_next_lecture_recommendation(user.id)
    if recommendation is None:
        raise NextLectureNotFoundException
    return recommendation


@router.get(
    "/courses/next/task",
    dependencies=[require_verified_email],
    responses=verified_responses(NextTaskRecommendation, NextTaskNotFoundException),
)
async def get_next_task(
    course_id: str | None = Query(None, description="Filter recommendations to a specific course"),
    lecture_id: str | None = Query(None, description="Filter recommendations to a specific lecture"),
    user: User = user_auth,
) -> Any:
    """Return the next unsolved quiz recommendation for the user."""

    if (course_id is None) != (lecture_id is None):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "course_id and lecture_id must be provided together"},
        )

    recommendation = await _resolve_next_task_recommendation(user.id, course_id=course_id, lecture_id=lecture_id)
    if recommendation is None:
        raise NextTaskNotFoundException
    return recommendation


@router.get(
    "/courses/next/lab",
    dependencies=[require_verified_email],
    responses=verified_responses(NextLabRecommendation, NextLabNotFoundException),
)
async def get_next_lab(
    course_id: str | None = Query(None, description="Filter recommendations to a specific course"),
    lecture_id: str | None = Query(None, description="Filter recommendations to a specific lecture"),
    user: User = user_auth,
) -> Any:
    """Return the next unsolved lab (coding/hacking) recommendation for the user."""

    if (course_id is None) != (lecture_id is None):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "course_id and lecture_id must be provided together"},
        )

    recommendation = await _resolve_next_lab_recommendation(user.id, course_id=course_id, lecture_id=lecture_id)
    if recommendation is None:
        raise NextLabNotFoundException
    return recommendation


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
