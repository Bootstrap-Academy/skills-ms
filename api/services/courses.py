from pathlib import Path

import pydantic
from yaml import safe_load

from api.logger import get_logger
from api.schemas.course import Course


logger = get_logger(__name__)


def _load_courses() -> dict[str, Course]:
    courses = {}
    for file in sorted(Path("config/courses").glob("*.yml")):
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
    logger.debug("course definitions are valid")


COURSES: dict[str, Course] = _load_courses()
_check_course_definitions()
