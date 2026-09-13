"""Course composition over existing rooms, lectures and challenge identities."""

from hashlib import sha256
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy.orm import load_only

from api.database import db, filter_by
from api.models import LectureProgress, PurchaseUser, RoomState
from api.schemas.course import Course
from api.schemas.course import Lecture as CourseLecture
from api.schemas.curriculum import (
    Activity,
    ActivityKind,
    ActivityReference,
    Curriculum,
    CurriculumChapter,
    CurriculumDefinition,
    LectureSource,
    LegacyPractice,
    Lesson,
    LessonDefinition,
    LessonSummary,
    RoomSource,
)
from api.schemas.rooms import Catalogue, CatalogueUnit, LocalizedText
from api.schemas.user import User
from api.services import rooms


def localized(value: str) -> LocalizedText:
    return LocalizedText(de=value, en=value)


def course_lectures(course: Course) -> dict[tuple[str, str], CourseLecture]:
    return {(section.id, lecture.id): lecture for section in course.sections for lecture in section.lectures}


def adapter_id(original: str, occupied: set[str], kind: str) -> str:
    """Only ambiguous presentation IDs are qualified; original state keys never change."""
    candidate = original
    while candidate in occupied:
        candidate = f"legacy-{kind}-" + sha256(candidate.encode()).hexdigest()[:32]
    occupied.add(candidate)
    return candidate


def definitions(course: Course, content: Catalogue | None = None) -> tuple[CurriculumDefinition, Catalogue | None]:
    if course.learning_path_id is None:
        content = None
    elif content is None:
        content = rooms.catalogue()
    units = {} if content is None else {unit.id: unit for unit in content.units if not unit.retired}
    if course.curriculum is not None:
        curriculum = course.curriculum
    else:
        chapters: list[CurriculumChapter] = []
        lessons: list[LessonDefinition] = []
        if content is not None:
            path = next((item for item in content.paths if item.id == course.learning_path_id), None)
            if path is None:
                raise HTTPException(404, "Dieser Kurs ist gerade nicht verfügbar.")
            chapters.extend(CurriculumChapter.parse_obj(chapter.dict()) for chapter in path.chapters)
            lessons.extend(
                LessonDefinition(
                    id=uid,
                    title=units[uid].title,
                    chapter_id=units[uid].chapter_id,
                    activities=[ActivityReference(id=uid, source=RoomSource(unit_id=uid))],
                )
                for uid in path.units
                if uid in units
            )
        chapter_ids = {chapter.id for chapter in chapters}
        lesson_ids = {lesson.id for lesson in lessons}
        activity_ids = {ref.id for lesson in lessons for ref in lesson.activities}
        for section in course.sections:
            chapter_id = adapter_id(section.id, chapter_ids, "chapter")
            chapters.append(CurriculumChapter(id=chapter_id, title=localized(section.title)))
            for lecture in section.lectures:
                lessons.append(
                    LessonDefinition(
                        id=adapter_id(lecture.id, lesson_ids, "lesson"),
                        title=localized(lecture.title),
                        chapter_id=chapter_id,
                        activities=[
                            ActivityReference(
                                id=adapter_id(lecture.id, activity_ids, "activity"),
                                source=LectureSource(course_id=course.id, section_id=section.id, lecture_id=lecture.id),
                            )
                        ],
                    )
                )
        # Legacy drafts may contain no lessons; the public adapter keeps them readable.
        if not lessons:
            curriculum = CurriculumDefinition.construct(chapters=chapters, lessons=[])
        else:
            try:
                curriculum = CurriculumDefinition(chapters=chapters, lessons=lessons)
            except ValueError:
                raise HTTPException(503, "Dieser Kurs kann gerade nicht geladen werden.") from None
    lectures = course_lectures(course)
    for lesson in curriculum.lessons:
        for activity in lesson.activities:
            source = activity.source
            if isinstance(source, RoomSource):
                unit = units.get(source.unit_id)
                if unit is None or unit.path_id != course.learning_path_id:
                    raise HTTPException(503, "Dieser Kurs kann gerade nicht geladen werden.")
            elif isinstance(source, LectureSource):
                if source.course_id != course.id or (source.section_id, source.lecture_id) not in lectures:
                    raise HTTPException(503, "Dieser Kurs kann gerade nicht geladen werden.")
    return curriculum, content


async def room_states(
    user: User, references: list[ActivityReference], *, summary: bool = False
) -> dict[str, RoomState]:
    guard = await db.get(PurchaseUser, user_id=user.id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "Dieses Konto ist nicht mehr verfügbar.")
    ids = {ref.source.unit_id for ref in references if isinstance(ref.source, RoomSource)}
    if not ids:
        return {}
    query = filter_by(RoomState, user_id=user.id).where(RoomState.unit_id.in_(ids))
    if summary:
        query = query.options(load_only(RoomState.unit_id, RoomState.status))
    rows = await db.all(query)
    return {row.unit_id: row for row in rows}


def completed(ref: ActivityReference, states: dict[str, RoomState], lectures: set[str]) -> bool:
    if isinstance(ref.source, RoomSource):
        row = states.get(ref.source.unit_id)
        return row is not None and row.status in ("completed", "skipped")
    if isinstance(ref.source, LectureSource):
        return ref.source.lecture_id in lectures
    # A summary never guesses a remote attempt result or issues a per-lesson fan-out.
    return False


async def get_curriculum(course: Course, user: User) -> Curriculum:
    definition, _ = definitions(course)
    refs = [activity for lesson in definition.lessons for activity in lesson.activities]
    states = await room_states(user, refs, summary=True)
    lectures = await LectureProgress.get_completed(user.id, course.id)
    return Curriculum(
        course_id=course.id,
        explicit=course.curriculum is not None,
        chapters=definition.chapters,
        lessons=[
            LessonSummary(
                id=lesson.id,
                title=lesson.title,
                chapter_id=lesson.chapter_id,
                activity_ids=[activity.id for activity in lesson.activities],
                completed=all(completed(activity, states, lectures) for activity in lesson.activities),
            )
            for lesson in definition.lessons
        ],
    )


async def completion_overrides(user: User | None, courses: Iterable[Course]) -> dict[str, bool | None]:
    """Batch explicit-course summaries without downloading activities or remote attempts."""
    explicit = [course for course in courses if course.curriculum is not None]
    if user is None or not explicit:
        return {}
    content: Catalogue | None = None
    curricula: dict[str, CurriculumDefinition] = {}
    unavailable: dict[str, bool | None] = {}
    for course in explicit:
        try:
            definition, loaded = definitions(course, content)
        except HTTPException as exc:
            if exc.status_code not in (404, 503):
                raise
            # One unavailable definition must not hide other course summaries.
            # An explicit null also prevents old video/path progress being used
            # as proof that this composed course has been completed.
            unavailable[course.id] = None
            continue
        curricula[course.id] = definition
        if loaded is not None:
            content = loaded
    refs = [
        activity for definition in curricula.values() for lesson in definition.lessons for activity in lesson.activities
    ]
    # Keep account-erasure and database checks outside the content fallback,
    # including when every requested curriculum is currently unavailable.
    states = await room_states(user, refs, summary=True)
    watched: dict[str, set[str]] = {}
    for row in await db.all(
        filter_by(LectureProgress, user_id=user.id).where(LectureProgress.course_id.in_(curricula))
    ):
        watched.setdefault(row.course_id, set()).add(row.lecture_id)
    return {
        **unavailable,
        **{
            course_id: all(
                completed(activity, states, watched.get(course_id, set()))
                for lesson in definition.lessons
                for activity in lesson.activities
            )
            for course_id, definition in curricula.items()
        },
    }


def room_kind(unit: CatalogueUnit) -> ActivityKind:
    if unit.room == "exercise":
        if unit.exercise is None:
            raise HTTPException(404, "Diese Aufgabe ist gerade nicht verfügbar.")
        kinds: dict[str, ActivityKind] = {"multiple_choice": "quiz", "matching": "matching", "coding": "coding"}
        return kinds[unit.exercise.type]
    if unit.room == "guided-lesson":
        return "explainer"
    if unit.room == "custom":
        return "custom"
    if unit.room == "video":
        return "video"
    return "legacy-room"


async def get_lesson(course: Course, lesson_id: str, user: User, token: str) -> Lesson:
    definition, content = definitions(course)
    lesson = next((item for item in definition.lessons if item.id == lesson_id), None)
    if lesson is None:
        raise HTTPException(404, "Diese Lektion gibt es nicht.")
    states = await room_states(user, lesson.activities)
    lecture_ids = await LectureProgress.get_completed(user.id, course.id)
    units = {} if content is None else {unit.id: unit for unit in content.units}
    lecture_map = course_lectures(course)
    activities = []
    for ref in lesson.activities:
        source = ref.source
        if isinstance(source, RoomSource):
            unit = units[source.unit_id]
            if unit.completion is None and unit.exercise is None:
                raise HTTPException(404, "Diese Aufgabe ist gerade nicht verfügbar.")
            await rooms.challenge_status(unit, user, token)
            public = await rooms.public_unit(unit)
            activities.append(
                Activity(
                    id=ref.id,
                    kind=room_kind(unit),
                    roles=ref.roles or (["practice"] if unit.exercise is not None else ["explanation"]),
                    title=ref.title or unit.title,
                    source=source,
                    content=public.content,
                    room=unit.room,
                    exercise=public.exercise,
                    module=public.module,
                    progress=rooms.progress(states.get(source.unit_id)),
                    completed=completed(ref, states, lecture_ids),
                    skip_allowed=unit.completion is not None and unit.completion.allow_skip,
                )
            )
        elif isinstance(source, LectureSource):
            lecture = lecture_map[source.section_id, source.lecture_id]
            activities.append(
                Activity(
                    id=ref.id,
                    kind="video",
                    roles=ref.roles or ["explanation"],
                    title=ref.title or localized(lecture.title),
                    source=source,
                    content=lecture.dict(),
                    completed=source.lecture_id in lecture_ids,
                )
            )
    practice = None
    if course.curriculum is None and len(lesson.activities) == 1:
        legacy_source = lesson.activities[0].source
        if isinstance(legacy_source, LectureSource):
            practice = LegacyPractice(
                course_id=course.id, section_id=legacy_source.section_id, lecture_id=legacy_source.lecture_id
            )
    return Lesson(
        course_id=course.id,
        explicit=course.curriculum is not None,
        id=lesson.id,
        title=lesson.title,
        chapter_id=lesson.chapter_id,
        activities=activities,
        completed=all(activity.completed is True for activity in activities),
        legacy_practice=practice,
    )
