"""Begin a lesson once; inspect the daily allowance without consuming it."""

from fastapi import APIRouter, Response

from api.auth import require_verified_email, user_auth
from api.endpoints.course import get_course, has_course_access
from api.schemas.course import Course
from api.schemas.daily_limit import DailyStatus, StartLesson, StartResult
from api.schemas.user import User
from api.services import daily_limit
from api.utils.docs import error_responses

router = APIRouter(dependencies=[require_verified_email], responses=error_responses(401, 403, 503))


@router.get("/daily-limit", response_model=DailyStatus)
async def daily_status(response: Response, user: User = user_auth) -> DailyStatus:
    response.headers["Cache-Control"] = "private, no-store"
    return await daily_limit.status(user)


@router.post(
    "/courses/{course_id}/lessons/{lesson_id}/start",
    response_model=StartResult,
    dependencies=[has_course_access],
    responses=error_responses(404, 409, 429),
)
async def start_lesson(
    lesson_id: str, data: StartLesson, response: Response, course: Course = get_course, user: User = user_auth
) -> StartResult:
    response.headers["Cache-Control"] = "private, no-store"
    lesson = daily_limit.lesson_definition(course, lesson_id)
    daily = await daily_limit.start(user, course, lesson, data.request_id)
    return StartResult(started=True, daily=daily)
