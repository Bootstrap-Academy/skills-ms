import pydantic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from yaml import safe_load

from api import models
from api.database import db
from api.logger import get_logger
from api.schemas.course import Course
from api.settings import settings


logger = get_logger(__name__)


async def get_owned_courses(user_id: str) -> set[str]:
    """Read paid and historical access through the committed admission pool."""
    # No cached negative or old request snapshot may hide a committed purchase.
    query = (
        select(models.CourseAccess.course_id)
        .where(models.CourseAccess.user_id == user_id)
        .union(select(models.LastWatch.course_id).where(models.LastWatch.user_id == user_id))
    )
    if db.admission_engine is None:
        raise RuntimeError("Committed course admission pool is unavailable")
    # This reserved pool performs only the short SELECT and never waits for a
    # slot in the outer request pool retained by callers waiting for admission.
    async with AsyncSession(db.admission_engine) as session:
        return set((await session.execute(query)).scalars())


def _load_courses() -> dict[str, Course]:
    courses = {}
    for file in sorted(settings.courses.glob("*.yml")):
        with file.open() as f:
            _id = file.name.removesuffix(".yml")
            logger.debug(f"loading course {_id}")
            courses[_id] = pydantic.parse_obj_as(Course, {"id": _id} | safe_load(f))
    return courses


def _check_course_definitions() -> None:
    for course in COURSES.values():
        sections = set()
        lectures = set()
        for section in course.sections:
            if section.id in sections:
                raise ValueError(f"Duplicate section id {section.id} in course {course.id}")
            sections.add(section.id)
            for lecture in section.lectures:
                if lecture.id in lectures:
                    raise ValueError(f"Duplicate lecture id {lecture.id} in course {course.id}")
                lectures.add(lecture.id)
                if (
                    lecture.type == "mp4"
                    and not settings.mp4_lectures.joinpath(course.id, lecture.id + ".mp4").is_file()
                ):
                    logger.warning(f"Missing mp4 lecture {lecture.id} in course {course.id}")
    logger.debug("course definitions are valid")


COURSES: dict[str, Course] = _load_courses()
_check_course_definitions()
