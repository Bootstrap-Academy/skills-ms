"""Endpoints related to courses and lectures"""

from typing import Any

from fastapi import APIRouter

from api.exceptions.course import CourseNotFoundException
from api.schemas.course import Course, CourseSummary
from api.services.courses import COURSES
from api.utils.docs import responses


router = APIRouter(tags=["course"])


@router.get("/courses", responses=responses(list[CourseSummary]))
async def list_courses() -> Any:
    return [course.summary for course in COURSES.values()]


@router.get("/courses/{course_id}", responses=responses(Course, CourseNotFoundException))
async def get_course(course_id: str) -> Any:
    if course_id not in COURSES:
        raise CourseNotFoundException
    return COURSES[course_id]
