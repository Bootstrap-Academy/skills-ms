"""One durable start per lesson; the existing account lock serializes admission.

GETs only inspect state. Historical progress is recognized without rewriting it;
its admission is materialized on the next mutation, or by the operator backfill.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, NamedTuple
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from api import models
from api.database import db, filter_by
from api.logger import get_logger
from api.schemas.course import Course
from api.schemas.curriculum import LectureSource, LessonDefinition, RoomSource
from api.schemas.daily_limit import ChallengeAdmission, DailyStatus, LearningHistory, LearningPolicy, LimitConfiguration
from api.schemas.rooms import Catalogue, CatalogueUnit
from api.schemas.user import User
from api.services.internal import InternalService, InternalServiceError
from api.services.purchases import lock_user, read_user_guard
from api.settings import settings
from api.utils.utc import utcnow

logger = get_logger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")


class AccessError(HTTPException):
    def __init__(self, status: int, code: str, detail: str, daily: DailyStatus | None = None):
        super().__init__(status, detail)
        self.code = code
        self.daily = daily


def day_window(now: datetime) -> tuple[date, datetime]:
    if now.tzinfo is None:
        raise ValueError("An aware instant is required")
    day = now.astimezone(BERLIN).date()
    return day, datetime.combine(day + timedelta(days=1), time(), BERLIN).astimezone(timezone.utc)


async def policy(user_id: str) -> LearningPolicy:
    if not settings.daily_limit_policy_enabled or user_id in db.session.info.get("retained_learning_subjects", set()):
        return LearningPolicy(mode="legacy", premium=False, single_course_sales=True, heart_sales=True)
    key = ("learning_policy", user_id)
    cached = db.session.info.get(key)
    if isinstance(cached, LearningPolicy):
        return cached
    if isinstance(cached, AccessError):
        raise cached
    try:
        async with InternalService.SHOP.client as client:
            response = await client.get(f"/learning-policy/{user_id}", timeout=3)
        if response.status_code == 404:
            raise HTTPException(401, "Dieses Konto ist nicht mehr verfügbar.")
        if response.status_code != 200:
            raise ValueError("Unexpected policy response")
        result = LearningPolicy.model_validate(response.json())
    except (httpx.HTTPError, InternalServiceError, ValueError, ValidationError):
        failure = AccessError(503, "learning_access_unavailable", "Dein Lernzugang ist gerade nicht erreichbar.")
        db.session.info[key] = failure
        raise failure from None
    db.session.info[key] = result
    return result


async def configuration() -> tuple[str, int]:
    cached: tuple[str, int] | None = db.session.info.get("daily_configuration")
    if cached is not None:
        return cached
    row = await db.get(models.DailyLimitSettings, id=1)
    result = ("off", 3) if row is None else (row.mode, row.limit)
    db.session.info["daily_configuration"] = result
    return result


def activation_issues() -> list[str]:
    from api.services.courses import COURSES
    from api.services.curriculum import definitions

    issues = []
    for course in COURSES.values():
        if course.learning_path_id is not None and course.curriculum is None:
            issues.append(f"curriculum_required:{course.id}")
        try:
            definitions(course)
        except HTTPException:
            issues.append(f"curriculum_unavailable:{course.id}")
    return issues


async def configure(data: LimitConfiguration) -> dict[str, Any]:
    issues = activation_issues() if data.mode == "enforce" else []
    if issues:
        raise HTTPException(409, {"code": "daily_limit_activation_blocked", "issues": issues})
    row = await db.get(models.DailyLimitSettings, id=1)
    values = {**data.model_dump(), "updated_at": utcnow()}
    if row is None:
        await db.add(models.DailyLimitSettings(id=1, **values))
    else:
        for name, value in values.items():
            setattr(row, name, value)
    await db.session.flush()
    db.session.info.pop("daily_configuration", None)
    logger.info("Daily lesson settings changed: mode=%s limit=%s operator=%s", data.mode, data.limit, data.updated_by)
    return {**data.model_dump(), "activation_issues": issues}


def lesson_definition(course: Course, lesson_id: str) -> LessonDefinition:
    from api.services.curriculum import definitions

    definition, _ = definitions(course)
    lesson = next((item for item in definition.lessons if item.id == lesson_id), None)
    if lesson is None:
        lesson = next(
            (
                item
                for item in definition.lessons
                if any(isinstance(a.source, RoomSource) and a.source.unit_id == lesson_id for a in item.activities)
            ),
            None,
        )
    if lesson is None:
        raise HTTPException(404, "Diese Lektion gibt es nicht.")
    return lesson


def lecture_lesson(course: Course, lecture_id: str, content: Catalogue | None = None) -> LessonDefinition:
    from api.services.curriculum import definitions

    definition, _ = definitions(course, content)
    for lesson in definition.lessons:
        if any(isinstance(a.source, LectureSource) and a.source.lecture_id == lecture_id for a in lesson.activities):
            return lesson
    raise HTTPException(404, "Diese Lektion gibt es nicht.")


def unit_lessons(
    unit_id: str, course_id: str | None = None, content: Catalogue | None = None
) -> list[tuple[Course, LessonDefinition]]:
    from api.services import rooms
    from api.services.courses import COURSES
    from api.services.curriculum import definitions

    content = content if content is not None else rooms.catalogue()
    unit = next((item for item in content.units if item.id == unit_id), None)
    if unit is None:
        return []
    result = []
    for course in sorted(COURSES.values(), key=lambda item: item.id):
        if course.learning_path_id != unit.path_id or (course_id is not None and course.id != course_id):
            continue
        definition, _ = definitions(course, content)
        for lesson in definition.lessons:
            if any(isinstance(a.source, RoomSource) and a.source.unit_id == unit_id for a in lesson.activities):
                result.append((course, lesson))
    return result


class AdmissionSnapshot(NamedTuple):
    starts: dict[tuple[str, str], models.LessonStart]
    purchases: set[str]
    rooms: dict[str, models.RoomState]
    lectures: set[tuple[str, str]]
    charged_by_day: dict[date, int]


async def snapshot(user: User) -> AdmissionSnapshot:
    key = ("daily_snapshot", user.id)
    cached = db.session.info.get(key)
    if isinstance(cached, AdmissionSnapshot):
        return cached

    async def rows(model: Any) -> list[Any]:
        query = filter_by(model, user_id=user.id)
        if db.session.info.get(("daily_locked", user.id)):
            query = query.with_for_update().execution_options(populate_existing=True)
        return await db.all(query)

    starts = {(row.course_id, row.lesson_id): row for row in await rows(models.LessonStart)}
    charged_by_day: dict[date, int] = {}
    for row in starts.values():
        if row.charged:
            charged_by_day[row.local_day] = charged_by_day.get(row.local_day, 0) + 1
    result = AdmissionSnapshot(
        starts,
        {row.course_id for row in await rows(models.CourseAccess)},
        {row.unit_id: row for row in await rows(models.RoomState)},
        {(row.course_id, row.lecture_id) for row in await rows(models.LectureProgress)},
        charged_by_day,
    )
    db.session.info[key] = result
    return result


@dataclass
class ReadCatalogue:
    content: Catalogue
    units: dict[str, CatalogueUnit]


def read_catalogue() -> ReadCatalogue:
    """Resolve admission identities once per database session without copying content."""
    from api.services import rooms

    cached: ReadCatalogue | None = db.session.info.get("daily_catalogue")
    if cached is None:
        content = rooms.catalogue(deep=False)
        cached = ReadCatalogue(content, {unit.id: unit for unit in content.units})
        db.session.info["daily_catalogue"] = cached
    return cached


async def purchased(user: User, course: Course) -> bool:
    # Preserve historical LastWatch access separately; it is not a purchase.
    return course.id in (await snapshot(user)).purchases


async def read_history_batch(user_id: str, payload: dict[str, Any]) -> LearningHistory:
    """Trusted read-only CH database evidence; never call its public Skills-gated API."""
    try:
        async with InternalService.CHALLENGES.client as client:
            response = await client.post(f"/users/{user_id}/learning-history", json=payload, timeout=5)
        if response.status_code != 200:
            raise ValueError("Unexpected history response")
        result = LearningHistory.model_validate(response.json())
        requested = set(payload["subtask_ids"])
        lectures = {(item["course_id"], item["lecture_id"]) for item in payload["lecture_bindings"]}
        if not {str(value) for value in result.attempted_subtask_ids}.issubset(requested) or not {
            (item.course_id, item.lecture_id) for item in result.attempted_lecture_bindings
        }.issubset(lectures):
            raise ValueError("Unrequested historical evidence")
        return result
    except (httpx.HTTPError, InternalServiceError, ValueError, ValidationError):
        raise AccessError(
            503, "learning_history_unavailable", "Deine bisherigen Lernschritte sind gerade nicht erreichbar."
        ) from None


async def challenge_history(user: User) -> tuple[set[UUID], set[tuple[str, str]]]:
    if not settings.daily_limit_policy_enabled:
        return set(), set()

    from api.services.courses import COURSES

    key = ("challenge_history", user.id)
    cached = db.session.info.get(key)
    if isinstance(cached, AccessError):
        raise cached
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    subtasks = (
        sorted(
            {
                str(unit.exercise.subtask_id)
                for unit in read_catalogue().content.units
                if not unit.retired and unit.exercise
            }
        )
        if settings.rooms_enabled
        else []
    )
    lectures = sorted(
        {
            (course.id, lecture.id)
            for course in COURSES.values()
            for section in course.sections
            for lecture in section.lectures
        }
    )
    entries: list[tuple[str, Any]] = [("subtask", item) for item in subtasks] + [("lecture", item) for item in lectures]
    attempted: set[UUID] = set()
    attempted_lectures: set[tuple[str, str]] = set()
    try:
        for offset in range(0, len(entries), 500):
            end = offset + 500
            batch = entries[offset:end]
            result = await read_history_batch(
                user.id,
                {
                    "subtask_ids": [value for kind, value in batch if kind == "subtask"],
                    "lecture_bindings": [
                        {"course_id": value[0], "lecture_id": value[1]} for kind, value in batch if kind == "lecture"
                    ],
                },
            )
            attempted.update(result.attempted_subtask_ids)
            attempted_lectures.update((item.course_id, item.lecture_id) for item in result.attempted_lecture_bindings)
    except AccessError as exc:
        db.session.info[key] = exc
        raise
    value = attempted, attempted_lectures
    db.session.info[key] = value
    return value


async def historical_progress(
    user: User, course: Course, lesson: LessonDefinition, *, with_history: bool = True
) -> bool:
    data = await snapshot(user)
    exercise_ids: set[UUID] = set()
    lecture_ids: set[tuple[str, str]] = set()
    for activity in lesson.activities:
        source = activity.source
        if isinstance(source, LectureSource):
            if (course.id, source.lecture_id) in data.lectures:
                return True
            lecture_ids.add((course.id, source.lecture_id))
        elif isinstance(source, RoomSource):
            # Pre-grouping lesson IDs were unit IDs. A begin without a saved
            # draft still preserves the new containing lesson after grouping.
            if (course.id, source.unit_id) in data.starts:
                return True
            row = data.rooms.get(source.unit_id)
            if row is not None and (
                row.status in ("in_progress", "completed")
                or bool(row.state)
                or (row.review_id is not None and row.review_status == "in_progress")
            ):
                return True
            if settings.rooms_enabled:
                unit = read_catalogue().units.get(source.unit_id)
                if unit is not None and unit.exercise is not None:
                    exercise_ids.add(unit.exercise.subtask_id)
    if with_history and (exercise_ids or lecture_ids):
        attempted, lectures = await challenge_history(user)
        return bool(exercise_ids & attempted or lecture_ids & lectures)
    return False


async def begun(user: User, course: Course, lesson: LessonDefinition) -> bool:
    return (course.id, lesson.id) in (await snapshot(user)).starts or await historical_progress(user, course, lesson)


async def status(user: User, course: Course | None = None, lesson: LessonDefinition | None = None) -> DailyStatus:
    guard = await read_user_guard(user.id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "Dieses Konto ist nicht mehr verfügbar.")
    current = await policy(user.id)
    mode, limit = await configuration()
    day, reset = day_window(utcnow())
    data = await snapshot(user)
    used = data.charged_by_day.get(day, 0)
    exempt: Literal["admin", "premium", "purchase", "started"] | None = (
        "admin" if user.admin else "premium" if current.premium else None
    )
    if exempt is None and course is not None and await purchased(user, course):
        exempt = "purchase"
    unlimited = exempt is not None
    started = (
        course is not None
        and lesson is not None
        and (
            (course.id, lesson.id) in data.starts
            or await historical_progress(user, course, lesson, with_history=not unlimited)
        )
    )
    if exempt is None and started:
        exempt = "started"
    enforced = current.mode == "daily" and mode == "enforce"
    remaining = max(0, limit - used) if enforced and not unlimited else None
    return DailyStatus(
        mode=current.mode,
        limit=limit,
        used=used,
        remaining=remaining,
        resets_at=reset,
        unlimited=unlimited,
        enforced=enforced,
        started=started,
        exempt=exempt,
        can_start=not enforced or exempt is not None or used < limit,
    )


def require_available(daily: DailyStatus) -> None:
    if not daily.can_start:
        raise AccessError(
            429,
            "daily_limit_reached",
            "Für heute hast du alle neuen Lektionen begonnen. Du kannst weiterüben oder Angefangenes fortsetzen.",
            daily,
        )


async def optional_status(
    user: User, course: Course | None = None, lesson: LessonDefinition | None = None
) -> DailyStatus | None:
    if not settings.daily_limit_policy_enabled:
        return None
    try:
        return await status(user, course, lesson)
    except AccessError as exc:
        if exc.status_code != 503:
            raise
        # Absence means unknown; never invent unlimited entitlement on outage.
        return None


async def choose(
    user: User, candidates: list[tuple[Course, LessonDefinition]]
) -> tuple[Course, LessonDefinition] | None:
    data = await snapshot(user)
    for course, lesson in candidates:
        if (
            (course.id, lesson.id) in data.starts
            or course.id in data.purchases
            or await historical_progress(user, course, lesson, with_history=False)
        ):
            return course, lesson
    for course, lesson in candidates:
        try:
            if await begun(user, course, lesson):
                return course, lesson
        except AccessError:
            # Selection grants no entitlement. Status/start keeps the unknown
            # state and decides whether evidence is required for enforcement.
            continue
    return candidates[0] if candidates else None


async def for_unit(user: User, unit_id: str, course_id: str | None = None) -> DailyStatus | None:
    if not settings.daily_limit_policy_enabled:
        return None
    pair = await choose(user, unit_lessons(unit_id, course_id))
    if pair is None:
        return None
    daily = await optional_status(user, *pair)
    if daily is None:
        from api.services.courses import get_owned_courses

        course, lesson = pair
        if not (course.free or user.admin or course.id in await get_owned_courses(user.id)):
            await course_daily_access(user, course, lesson)
    return daily


async def confirmed_policy_mode(user: User) -> str | None:
    try:
        return (await policy(user.id)).mode
    except AccessError:
        return None


async def heart_policy(user: User, pair: tuple[Course, LessonDefinition] | None) -> Literal["daily", "legacy"] | None:
    """Access alone never proves free billing under an unavailable policy."""
    current = await confirmed_policy_mode(user)
    if current is not None:
        return "daily" if current == "daily" else "legacy"
    if pair is None:
        return None
    course, lesson = pair
    ids = {lesson.id} | {
        activity.source.unit_id for activity in lesson.activities if isinstance(activity.source, RoomSource)
    }
    rows = [
        row
        for (course_id, lesson_id), row in (await snapshot(user)).starts.items()
        if course_id == course.id and lesson_id in ids
    ]
    if any(row.policy_mode == "daily" or (row.policy_mode is None and row.reason == "daily") for row in rows):
        return "daily"
    if any(row.policy_mode in ("legacy", "shadow") for row in rows):
        return "legacy"
    return None


async def start(
    user: User, course: Course, lesson: LessonDefinition, request_id: UUID | None = None, *, locked: bool = False
) -> DailyStatus | None:
    if not settings.daily_limit_policy_enabled:
        return None
    if not locked:
        guard = await lock_user(user.id)
        if guard.deleted:
            raise HTTPException(401, "Dieses Konto ist nicht mehr verfügbar.")
    db.session.info.pop(("daily_snapshot", user.id), None)
    db.session.info[("daily_locked", user.id)] = True
    if request_id is not None:
        receipt = await db.first(
            filter_by(models.LessonStartRequest, user_id=user.id, request_id=str(request_id))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if receipt is not None:
            if (receipt.course_id, receipt.lesson_id) != (course.id, lesson.id):
                raise AccessError(409, "request_id_conflict", "Diese Anfrage gehört zu einer anderen Lektion.")
            row = (await snapshot(user)).starts.get((course.id, lesson.id))
            if row is not None and (confirmed := await confirmed_policy_mode(user)) is not None:
                row.policy_mode = confirmed
            return await optional_status(user, course, lesson)
    daily = await optional_status(user, course, lesson)
    if daily is None and not (course.free or user.admin):
        # A course-level browse permission must not open unrelated new work
        # while policy is unknown. Keep only separately proven access.
        from api.services.courses import get_owned_courses

        if course.id not in await get_owned_courses(user.id) and not await begun(user, course, lesson):
            raise AccessError(503, "learning_access_unavailable", "Dein Lernzugang ist gerade nicht erreichbar.")
    mode, _ = await configuration()
    if daily is not None:
        require_available(daily)
        # Disabled technical mode and old contracts leave legacy behavior exact.
        if daily.mode == "legacy" and mode == "off":
            return daily
    row = (await snapshot(user)).starts.get((course.id, lesson.id))
    confirmed = await confirmed_policy_mode(user)
    if row is None:
        try:
            old = await historical_progress(user, course, lesson, with_history=not (daily and daily.unlimited))
        except AccessError:
            try:
                enforcing = mode == "enforce" and (await policy(user.id)).mode == "daily"
            except AccessError:
                enforcing = False
            if enforcing:
                raise
            # Disabled/measurement/legacy admission must not become a new
            # historical-service dependency. Unknown starts are uncharged.
            old = False
        charge = daily is not None and not old and not daily.unlimited and mode != "off"
        reason = (
            "historical"
            if old
            else (
                "service_unavailable"
                if daily is None
                else daily.exempt or ("daily" if daily.enforced else "shadow" if charge else "off")
            )
        )
        now = utcnow()
        await db.add(
            models.LessonStart(
                user_id=user.id,
                course_id=course.id,
                lesson_id=lesson.id,
                started_at=now,
                local_day=day_window(now)[0],
                charged=charge,
                reason=reason,
                policy_mode=confirmed,
            )
        )
    elif confirmed is not None:
        row.policy_mode = confirmed
    if request_id is not None:
        await db.add(
            models.LessonStartRequest(
                user_id=user.id,
                request_id=str(request_id),
                course_id=course.id,
                lesson_id=lesson.id,
                created_at=utcnow(),
            )
        )
    await db.session.flush()
    db.session.info.pop(("daily_snapshot", user.id), None)
    return await optional_status(user, course, lesson)


async def start_unit(user: User, unit_id: str, course_id: str | None, *, locked: bool = False) -> DailyStatus | None:
    if not settings.daily_limit_policy_enabled:
        return None
    pair = await choose(user, unit_lessons(unit_id, course_id))
    return None if pair is None else await start(user, *pair, locked=locked)


async def daily_course_access(user: User) -> bool:
    if not settings.daily_limit_policy_enabled:
        return False
    return (await policy(user.id)).mode == "daily"


async def course_daily_access(user: User, course: Course, lesson: LessonDefinition | None = None) -> bool:
    """Keep unknown policy distinct from denial, with scoped continuation rights."""
    try:
        return await daily_course_access(user)
    except AccessError:
        if lesson is not None:
            if await begun(user, course, lesson):
                return True
        else:
            from api.services.curriculum import definitions

            lessons = definitions(course)[0].lessons
            data = await snapshot(user)
            for item in lessons:
                if (course.id, item.id) in data.starts or await historical_progress(
                    user, course, item, with_history=False
                ):
                    return True
            for item in lessons:
                if await begun(user, course, item):
                    return True
        raise


@dataclass
class ChallengeReadContext:
    """One read-only HTTP batch; never shared with starts or later requests.

    Resolve each task/subtask independently before reusing a decision for the
    same ordered course/lesson scopes. Sibling lessons retain separate decisions
    during policy outages. No task-wide entitlement or persistent cache exists.
    """

    content: Catalogue | None = None
    task_units: dict[UUID, list[CatalogueUnit]] | None = None
    unit_pairs: dict[str, list[tuple[Course, LessonDefinition]]] = field(default_factory=dict)
    lectures: dict[tuple[str, str], LessonDefinition] = field(default_factory=dict)
    decisions: dict[tuple[str, bool, tuple[tuple[str, str], ...], tuple[str, ...]], dict[str, Any] | HTTPException] = (
        field(default_factory=dict)
    )

    def catalogue(self) -> Catalogue:
        from api.services import rooms

        if self.content is None:
            self.content = rooms.catalogue(deep=False)
        return self.content

    def for_unit(self, unit_id: str) -> list[tuple[Course, LessonDefinition]]:
        if unit_id not in self.unit_pairs:
            self.unit_pairs[unit_id] = unit_lessons(unit_id, content=self.catalogue())
        return self.unit_pairs[unit_id]

    def for_task(self, task_id: UUID, subtask_id: UUID | None) -> list[CatalogueUnit]:
        if self.task_units is None:
            self.task_units = {}
            for unit in self.catalogue().units:
                if not unit.retired and unit.exercise is not None:
                    self.task_units.setdefault(unit.exercise.task_id, []).append(unit)
        return [
            unit
            for unit in self.task_units.get(task_id, [])
            if unit.exercise is not None and (subtask_id is None or unit.exercise.subtask_id == subtask_id)
        ]

    def for_lecture(self, course: Course, lecture_id: str) -> LessonDefinition:
        key = (course.id, lecture_id)
        if key not in self.lectures:
            content = self.catalogue() if course.learning_path_id is not None else None
            self.lectures[key] = lecture_lesson(course, lecture_id, content)
        return self.lectures[key]


async def challenge_admission(
    user_id: str, data: ChallengeAdmission, mutate: bool, read_context: ChallengeReadContext | None = None
) -> dict[str, Any]:
    from api.services import rooms
    from api.services.courses import COURSES

    user = User(id=user_id, admin=data.user_admin, email_verified=True)
    if mutate and data.request_id is None:
        raise HTTPException(422, "request_id is required")
    if mutate and read_context is not None:
        raise ValueError("A read batch cannot start lessons")
    candidates: list[tuple[Course, LessonDefinition]] = []
    if settings.rooms_enabled and data.task_id is not None:
        units = (
            read_context.for_task(data.task_id, data.subtask_id)
            if read_context is not None
            else rooms.catalogue().units
        )
        for unit in units:
            if (
                not unit.retired
                and unit.exercise is not None
                and unit.exercise.task_id == data.task_id
                and (data.subtask_id is None or unit.exercise.subtask_id == data.subtask_id)
            ):
                candidates.extend(read_context.for_unit(unit.id) if read_context is not None else unit_lessons(unit.id))
    broad_courses = []
    for binding in data.lecture_bindings:
        course = COURSES.get(binding.course_id)
        if course is None or (
            binding.section_id is not None and not any(section.id == binding.section_id for section in course.sections)
        ):
            raise HTTPException(404, "Dieser Kurs ist gerade nicht verfügbar.")
        if binding.lecture_id is not None:
            if binding.section_id is not None and not any(
                section.id == binding.section_id
                and any(lecture.id == binding.lecture_id for lecture in section.lectures)
                for section in course.sections
            ):
                raise HTTPException(404, "Diese Lektion gibt es nicht.")
            lesson = (
                read_context.for_lecture(course, binding.lecture_id)
                if read_context is not None
                else lecture_lesson(course, binding.lecture_id)
            )
            candidates.append((course, lesson))
        else:
            broad_courses.append(course)
    if read_context is None:
        return await _challenge_scope_admission(user, candidates, broad_courses, mutate, data.request_id)
    key = (
        user.id,
        user.admin,
        tuple((c.id, lesson.id) for c, lesson in candidates),
        tuple(c.id for c in broad_courses),
    )
    if key not in read_context.decisions:
        try:
            read_context.decisions[key] = await _challenge_scope_admission(
                user, candidates, broad_courses, False, None, read_only=True
            )
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            read_context.decisions[key] = exc
    result = read_context.decisions[key]
    if isinstance(result, HTTPException):
        raise result
    return result


async def _challenge_scope_admission(
    user: User,
    candidates: list[tuple[Course, LessonDefinition]],
    broad_courses: list[Course],
    mutate: bool,
    request_id: UUID | None,
    *,
    read_only: bool = False,
) -> dict[str, Any]:
    from api.services.courses import get_owned_courses
    from api.services.shop import has_premium

    if not candidates and not broad_courses:
        return {"allowed": True, "lesson": None, "daily": None, "heart_policy": await heart_policy(user, None)}

    async def admitted(course: Course, lesson: LessonDefinition | None = None) -> bool:
        return (
            course.free
            or user.admin
            or course.id in await get_owned_courses(user.id)
            or await course_daily_access(user, course, lesson)
            or await has_premium(user.id)
        )

    pairs = []
    for course, lesson in candidates:
        if await admitted(course, lesson):
            pairs.append((course, lesson))
    broad_access = False
    for course in broad_courses:
        broad_access = await admitted(course) or broad_access
    if read_only:
        if not pairs and not broad_access:
            raise HTTPException(403, "Für diese Aufgabe brauchst du Zugang zum Kurs.")
        if settings.daily_limit_policy_enabled:
            guard = await read_user_guard(user.id)
            if guard is not None and guard.deleted:
                raise HTTPException(401, "Dieses Konto ist nicht mehr verfügbar.")
        # Read batches return permissions only. Choosing a preferred start,
        # quota display and billing evidence cannot change an admitted scope.
        # Keep the policy's deleted-account refusal and tolerate only the same
        # 503 uncertainty as the detail check's optional presentation fields.
        await confirmed_policy_mode(user)
        return {"allowed": True}
    pair = await choose(user, pairs)
    if pair is None:
        if not broad_access:
            raise HTTPException(403, "Für diese Aufgabe brauchst du Zugang zum Kurs.")
        # Course-/section-wide historical exercises have no defined lesson.
        # The course admission still applies; they remain free practice rather
        # than inventing one quota unit per quiz or blocking the learner.
        daily = await optional_status(user)
        return {"allowed": True, "lesson": None, "daily": daily, "heart_policy": await heart_policy(user, None)}
    daily = await start(user, *pair, request_id) if mutate else await optional_status(user, *pair)
    return {
        "allowed": True,
        "lesson": {"course_id": pair[0].id, "lesson_id": pair[1].id},
        "daily": daily,
        "heart_policy": await heart_policy(user, pair),
    }


async def backfill_user(user_id: str) -> int:
    """Materialize known historical progress; never infer a start from a skip."""
    if not settings.daily_limit_policy_enabled:
        return 0

    from api.services.courses import COURSES
    from api.services.curriculum import definitions

    guard = await lock_user(user_id)
    if guard.deleted:
        return 0
    user = User(id=user_id, admin=False, email_verified=True)
    db.session.info[("daily_locked", user.id)] = True
    db.session.info.pop(("daily_snapshot", user.id), None)
    data = await snapshot(user)
    now = utcnow()
    count = 0
    for course in COURSES.values():
        definition, _ = definitions(course)
        for lesson in definition.lessons:
            if (course.id, lesson.id) not in data.starts and await historical_progress(user, course, lesson):
                await db.add(
                    models.LessonStart(
                        user_id=user.id,
                        course_id=course.id,
                        lesson_id=lesson.id,
                        started_at=now,
                        local_day=day_window(now)[0],
                        charged=False,
                        reason="historical",
                    )
                )
                count += 1
    await db.session.flush()
    db.session.info.pop(("daily_snapshot", user.id), None)
    return count
