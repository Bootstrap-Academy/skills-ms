"""Endpoints related to courses and lectures"""

from typing import Any

from fastapi import APIRouter, Depends

from api import models
from api.auth import require_verified_email, user_auth
from api.database import db, filter_by
from api.exceptions.auth import UserNotFoundError, email_verified_responses
from api.exceptions.course import (
    AlreadyPurchasedCourseException,
    CourseIsFreeException,
    CourseNotFoundException,
    NoCourseAccessException,
)
from api.schemas.course import Course, CourseSummary
from api.schemas.user import User
from api.services.auth import exists_user
from api.services.courses import COURSES
from api.utils.docs import responses


router = APIRouter(tags=["course"])


@Depends
async def get_course(course_id: str) -> Course:
    if course_id not in COURSES:
        raise CourseNotFoundException
    return COURSES[course_id]


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
    responses=email_verified_responses(Course, NoCourseAccessException, CourseNotFoundException),
)
async def get_course_details(course: Course = get_course) -> Any:
    """
    Return details about a specific course.

    For premium courses the user must have access to the course.

    *Requirements:* **VERIFIED**
    """

    return course


@router.post(
    "/courses/{course_id}/{user_id}/access",
    dependencies=[require_verified_email],
    responses=responses(bool, CourseIsFreeException, AlreadyPurchasedCourseException, UserNotFoundError),
)
async def buy_course(user_id: str, course: Course = get_course) -> Any:
    """
    Buy access to a course for a user.

    *Requirements:* **VERIFIED**
    """

    if course.free:
        raise CourseIsFreeException

    if await db.exists(filter_by(models.CourseAccess, user_id=user_id, course_id=course.id)):
        raise AlreadyPurchasedCourseException

    if not await exists_user(user_id):
        raise UserNotFoundError

    await models.CourseAccess.create(user_id, course.id)

    return True
