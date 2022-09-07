"""Endpoints related to courses and lectures"""

from typing import Any

from fastapi import APIRouter

from api.auth import require_verified_email
from api.exceptions.auth import email_verified_responses
from api.exceptions.course import CourseNotFoundException
from api.schemas.course import Course, CourseSummary
from api.services.courses import COURSES
from api.utils.docs import responses


router = APIRouter(tags=["course"])


@router.get("/courses", responses=responses(list[CourseSummary]))
async def list_courses() -> Any:
    """Return a list of all available courses."""

    return [course.summary for course in COURSES.values()]


@router.get(
    "/courses/{course_id}",
    dependencies=[require_verified_email],
    responses=email_verified_responses(Course, CourseNotFoundException),
)
async def get_course(course_id: str) -> Any:
    """
    Return details about a specific course.

    *Requirements:* **VERIFIED**
    """

    if course_id not in COURSES:
        raise CourseNotFoundException

    return COURSES[course_id]
