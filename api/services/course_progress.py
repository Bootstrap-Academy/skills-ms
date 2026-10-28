"""Progress of a learner per course, by the completion rules the course summaries already apply.

- A course with an explicit curriculum is done when every activity of it is (`curriculum.completion_overrides`);
  its units are its lessons.
- A course on a learning path of the rooms is done when every active unit of the path is completed or skipped
  (`rooms.path_completed`); its units are those units. Without the rooms its progress is unknown.
- A video course is done when it has sections and every lecture is watched (`Course.summary`); its units are its
  lectures.

Reads only; nothing is written or derived beyond these rules.
"""

from dataclasses import dataclass
from typing import Iterable, Literal

from fastapi import HTTPException

from api.database import db, filter_by
from api.models import LectureProgress, RoomState
from api.schemas.course import Course
from api.schemas.curriculum import RoomSource
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import curriculum, rooms
from api.services.courses import COURSES
from api.settings import settings

Status = Literal["done", "in_progress", "open"]


@dataclass(frozen=True)
class CourseProgress:
    status: Status
    # Both null when the course has no units to count, or they cannot be read right now.
    done_units: int | None
    total_units: int | None


def _result(completed: bool, started: bool, done: int, total: int) -> CourseProgress:
    status: Status = "done" if completed else "in_progress" if started or done else "open"
    return CourseProgress(status, done if total else None, total if total else None)


def _explicit(
    course: Course, content: Catalogue | None, states: dict[str, RoomState], lectures: set[str]
) -> CourseProgress:
    try:
        definition, _ = curriculum.definitions(course, content)
    except HTTPException:
        # The course summary reports an unavailable composed course as "not known", never as completed.
        return CourseProgress("open", None, None)
    activities = [
        [curriculum.completed(ref, states, lectures) for ref in lesson.activities] for lesson in definition.lessons
    ]
    started = any(
        isinstance(ref.source, RoomSource) and ref.source.unit_id in states
        for lesson in definition.lessons
        for ref in lesson.activities
    )
    return _result(
        all(done for lesson in activities for done in lesson),
        started or any(done for lesson in activities for done in lesson),
        sum(all(lesson) for lesson in activities),
        len(activities),
    )


def _path(course: Course, content: Catalogue | None, states: dict[str, RoomState]) -> CourseProgress:
    path_id = course.learning_path_id
    if content is None or path_id is None:
        return CourseProgress("open", None, None)
    active = [unit.id for unit in content.units if unit.path_id == path_id and not unit.retired]
    finished = [uid for uid in active if uid in states and states[uid].status in ("completed", "skipped")]
    started = any(uid in states and states[uid].status != "new" for uid in active)
    return _result(rooms.path_completed(content, states, path_id), started, len(finished), len(active))


def _video(course: Course, lectures: set[str]) -> CourseProgress:
    ids = [lecture.id for section in course.sections for lecture in section.lectures]
    watched = sum(lecture_id in lectures for lecture_id in ids)
    return _result(bool(course.sections) and watched == len(ids), False, watched, len(ids))


async def course_progress(user: User, course_ids: Iterable[str]) -> dict[str, CourseProgress]:
    """Progress of `user` in each known course of `course_ids`; unknown ids are left out."""
    courses = [COURSES[course_id] for course_id in sorted(set(course_ids)) if course_id in COURSES]
    if not courses:
        return {}
    watched: dict[str, set[str]] = {}
    for row in await db.all(
        filter_by(LectureProgress, user_id=user.id).where(LectureProgress.course_id.in_([c.id for c in courses]))
    ):
        watched.setdefault(row.course_id, set()).add(row.lecture_id)
    content: Catalogue | None = None
    states: dict[str, RoomState] = {}
    if settings.rooms_enabled and any(course.learning_path_id is not None for course in courses):
        try:
            content = rooms.catalogue(deep=False)
        except HTTPException:
            content = None
        else:
            states = await rooms.read_states(user.id)
    result = {}
    for course in courses:
        lectures = watched.get(course.id, set())
        if course.curriculum is not None:
            result[course.id] = _explicit(course, content, states, lectures)
        elif course.learning_path_id is not None:
            result[course.id] = _path(course, content, states)
        else:
            result[course.id] = _video(course, lectures)
    return result
