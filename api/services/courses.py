from pathlib import Path

import pydantic
from yaml import safe_load

from api.schemas.course import Course


def _load_courses() -> dict[str, Course]:
    courses = {}
    for file in sorted(Path("config/courses").glob("*.yml")):
        with file.open() as f:
            _id = file.name.removesuffix(".yml")
            courses[_id] = pydantic.parse_obj_as(Course, {"id": _id} | safe_load(f))
    return courses


COURSES: dict[str, Course] = _load_courses()
