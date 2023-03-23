from fastapi import APIRouter

from api.schemas.course import Course
from api.services.courses import COURSES
from api.utils.docs import responses


router = APIRouter()


@router.get("/courses", responses=responses(dict[str, Course]))
async def get_courses() -> dict[str, Course]:
    """Return a list of all courses."""

    return COURSES
