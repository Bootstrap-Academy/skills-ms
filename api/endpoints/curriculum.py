"""Ordered lessons shared by the Academy player and future mobile clients."""

from fastapi import APIRouter, Request, Response

from api.auth import get_token, require_verified_email, user_auth
from api.endpoints.course import get_course, has_course_access
from api.schemas.course import Course
from api.schemas.curriculum import Curriculum, Lesson
from api.schemas.user import User
from api.services import curriculum


router = APIRouter(dependencies=[require_verified_email, has_course_access])


@router.get("/courses/{course_id}/curriculum", response_model=Curriculum)
async def get_curriculum(response: Response, course: Course = get_course, user: User = user_auth) -> Curriculum:
    response.headers["Cache-Control"] = "private, no-store"
    return await curriculum.get_curriculum(course, user)


@router.get("/courses/{course_id}/lessons/{lesson_id}", response_model=Lesson)
async def get_lesson(
    lesson_id: str, request: Request, response: Response, course: Course = get_course, user: User = user_auth
) -> Lesson:
    response.headers["Cache-Control"] = "private, no-store"
    return await curriculum.get_lesson(course, lesson_id, user, get_token(request))
