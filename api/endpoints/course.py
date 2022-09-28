"""Endpoints related to courses and lectures"""

from secrets import token_urlsafe
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import verified_responses
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
from api.schemas.course import Course, CourseSummary, Lecture, UserCourse
from api.schemas.user import User
from api.services.courses import COURSES
from api.services.shop import spend_coins
from api.settings import settings
from api.utils.docs import responses


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
    if not await db.exists(filter_by(models.CourseAccess, user_id=user.id, course_id=course.id)):
        raise NoCourseAccessException


@router.get("/courses", responses=responses(list[CourseSummary]))
async def list_courses() -> Any:
    """Return a list of all available courses."""

    return [course.summary for course in COURSES.values()]


@router.get(
    "/courses/{course_id}",
    dependencies=[require_verified_email, has_course_access],
    responses=verified_responses(UserCourse, NoCourseAccessException, CourseNotFoundException),
)
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
async def get_mp4_lecture_link(request: Request, course: Course = get_course, lecture: Lecture = get_lecture) -> Any:
    """
    Return the download link of an mp4 lecture.

    *Requirements:* **VERIFIED**
    """

    print(lecture)

    if lecture.type != "mp4":
        raise LectureNotFoundException

    path = settings.mp4_lectures.joinpath(course.id, lecture.id + ".mp4")
    if not path.is_file():
        raise LectureNotFoundException

    token = token_urlsafe(64)
    name = f"{course.id}_{lecture.id}.mp4"
    await redis.setex(f"mp4_lecture:{token}:{name}", 60, str(path))

    return f"{settings.public_base_url.rstrip('/')}/lectures/{token}/{name}"


@router.get("/lectures/{token}/{file}", include_in_schema=False)
async def download_mp4_lecture(token: str, file: str) -> Any:
    path = await redis.get(f"mp4_lecture:{token}:{file}")
    if not path:
        raise LectureNotFoundException

    return FileResponse(path, media_type="video/mp4", filename=file)


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

    return True


@router.get("/course_access", dependencies=[require_verified_email], responses=verified_responses(list[CourseSummary]))
async def get_accessible_courses(user: User = user_auth) -> Any:
    """
    Return a list of all courses the user has access to.

    *Requirements:* **VERIFIED**
    """

    course_ids = {k for k, v in COURSES.items() if v.free or user.admin}
    async for course_access in await db.stream(filter_by(models.CourseAccess, user_id=user.id)):
        if course_access.course_id in COURSES:
            course_ids.add(course_access.course_id)
    return [COURSES[course_id].summary for course_id in course_ids]


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

    if not await spend_coins(user.id, course.price):
        raise NotEnoughCoinsError

    await models.CourseAccess.create(user.id, course.id)

    return True
