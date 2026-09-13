"""Curated learning flow with private state; never awards XP or changes courses."""

import json
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from api.database import db, delete, filter_by
from api.models import PurchaseUser
from api.models.room import RoomRequest, RoomState
from api.schemas.course import Course
from api.schemas.rooms import (
    Catalogue,
    CataloguePath,
    CatalogueUnit,
    Complete,
    CourseLearning,
    CourseLearningUnit,
    Exercise,
    LearningPath,
    Progress,
    Result,
    RoomEnvelope,
    Rooms,
    SaveState,
    StartReview,
    Unit,
)
from api.schemas.user import User
from api.services.courses import COURSES, get_owned_courses
from api.services.lesson_modules import resolve_module
from api.services.purchases import lock_user
from api.services.shop import has_premium
from api.settings import settings
from api.utils.utc import utcnow


@lru_cache(maxsize=1)
def load_catalogue() -> Catalogue:
    try:
        path = settings.learning_rooms_content or Path(__file__).parents[1].joinpath("content/learning_rooms.json")
        if settings.learning_rooms_content is not None and (
            not path.is_absolute() or not path.is_file() or path.is_symlink()
        ):
            raise ValueError("Private catalogue must be an absolute regular file")
        return Catalogue.parse_raw(path.read_text())
    except (OSError, ValueError, ValidationError):
        raise HTTPException(503, "Learning rooms are temporarily unavailable") from None


def catalogue() -> Catalogue:
    if not settings.rooms_enabled:
        raise HTTPException(404, "Learning rooms are unavailable")
    content = load_catalogue().copy(deep=True)
    by_id = {unit.id: unit for unit in content.units}
    try:
        for unit_id, reference in settings.learning_rooms_exercise_refs.items():
            if unit_id not in by_id or by_id[unit_id].room not in ("exercise", "custom", "video"):
                raise ValueError("Unknown exercise mapping")
            if by_id[unit_id].completion is not None:
                raise ValueError("An activity cannot have two completion authorities")
            by_id[unit_id].exercise = Exercise.parse_obj(reference)
    except (ValueError, ValidationError):
        raise HTTPException(503, "Learning-room exercises are temporarily unavailable") from None
    return content


async def read_states(user_id: str, unit_ids: set[str] | None = None) -> dict[str, RoomState]:
    # Reads never create the durable user lock or a working-state row.
    guard = await db.get(PurchaseUser, user_id=user_id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    query = filter_by(RoomState, user_id=user_id).execution_options(populate_existing=True)
    if unit_ids is not None:
        query = query.where(RoomState.unit_id.in_(unit_ids))
    return {row.unit_id: row for row in await db.all(query)}


def progress(row: RoomState | None) -> Progress:
    if row is None:
        return Progress()
    reviewing = row.review_id is not None
    status = row.review_status if reviewing else row.status
    result = row.result
    if reviewing and status == "completed" and row.status == "skipped":
        # Only introductions can originally be skipped. Completing their review
        # introduces the concept without rewriting the original achievement.
        result = {"kind": "introduced"}
    return Progress.parse_obj(
        {
            "revision": row.revision,
            "state": row.state,
            "status": status,
            "result": result if status == "completed" else None,
            "review_id": row.review_id,
        }
    )


def introduced_concepts(content: Catalogue, states: dict[str, RoomState]) -> set[str]:
    concepts: set[str] = set()
    for unit in content.units:
        row = states.get(unit.id)
        if row is None:
            continue
        if row.status == "skipped" and unit.completion is not None:
            concepts.update(unit.teaches)
        elif row.status == "completed" and row.result == {"kind": "introduced"}:
            concepts.update(unit.teaches)
        elif row.status == "completed" and row.result == {"kind": "solved"}:
            concepts.update([*unit.teaches, *unit.practices])
    return concepts


def find_unit(
    content: Catalogue, unit_id: str, states: dict[str, RoomState], *, prerequisites: bool = True
) -> CatalogueUnit:
    unit = next((unit for unit in content.units if unit.id == unit_id and not unit.retired), None)
    if unit is None or (unit.completion is None and unit.exercise is None):
        raise HTTPException(404, "This learning room is unavailable")
    if prerequisites and not set(unit.requires).issubset(introduced_concepts(content, states)):
        raise HTTPException(403, "Start with the introduction for this room")
    return unit


async def public_unit(unit: CatalogueUnit, user: User | None = None, course_id: str | None = None) -> Unit:
    public = unit.public()
    if unit.room == "custom":
        if unit.module_id is None:
            raise HTTPException(503, "Diese Lektion kann gerade nicht geladen werden.")
        public.module = await resolve_module(unit.module_id, user=user, unit_id=unit.id, course_id=course_id)
    return public


async def accessible_paths(content: Catalogue, user: User) -> set[str]:
    """A course link never bypasses the existing paid-course admission rule."""
    linked: dict[str, list[Course]] = {}
    for course in COURSES.values():
        if course.learning_path_id is not None:
            linked.setdefault(course.learning_path_id, []).append(course)
    accessible = {
        path.id
        for path in content.paths
        if path.id not in linked or user.admin or any(course.free for course in linked[path.id])
    }
    restricted = {path.id for path in content.paths} - accessible
    if not restricted:
        return accessible
    owned = await get_owned_courses(user.id)
    accessible.update(pid for pid in restricted if any(course.id in owned for course in linked[pid]))
    if restricted - accessible and await has_premium(user.id):
        accessible.update(restricted)
    return accessible


async def require_path_access(content: Catalogue, path_id: str, user: User, course_id: str | None = None) -> None:
    if course_id is not None:
        course = COURSES.get(course_id)
        if course is None or course.learning_path_id != path_id:
            raise HTTPException(404, "This lesson is not part of that course")
        if course.free or user.admin or course.id in await get_owned_courses(user.id) or await has_premium(user.id):
            return
        raise HTTPException(403, "Open the course to get access to these lessons")
    requested = content.copy(update={"paths": [path for path in content.paths if path.id == path_id]})
    if path_id not in await accessible_paths(requested, user):
        raise HTTPException(403, "Open the course to get access to these lessons")


async def challenge_status(unit: CatalogueUnit, user: User, token: str) -> bool:
    """Read the existing challenge authority through one operator-configured service.

    No client/content URL, redirects, solution endpoint, cached identity, internal
    admin credential, submission, purchase or reward call participates here.
    """
    exercise = unit.exercise
    if exercise is None:
        return False
    resources = {"multiple_choice": "multiple_choice", "matching": "matchings", "coding": "coding_challenges"}
    expected_types = {
        "multiple_choice": "MULTIPLE_CHOICE_QUESTION",
        "matching": "MATCHING",
        "coding": "CODING_CHALLENGE",
    }
    url = (
        settings.challenges_url.rstrip("/")
        + f"/tasks/{exercise.task_id}/{resources[exercise.type]}/{exercise.subtask_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            raise HTTPException(401, "Please sign in again")
        if response.status_code in (403, 404):
            raise HTTPException(404, "This exercise is unavailable")
        if response.status_code != 200:
            raise HTTPException(503, "The exercise could not be checked")
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("id") != str(exercise.subtask_id)
            or data.get("task_id") != str(exercise.task_id)
            or data.get("type") != expected_types[exercise.type]
            or not isinstance(data.get("solved"), bool)
            or not isinstance(data.get("enabled"), bool)
            or not isinstance(data.get("retired"), bool)
            or not isinstance(data.get("creator"), str)
        ):
            raise HTTPException(503, "The exercise could not be checked")
        if not data["enabled"] or data["retired"] or data["creator"] == user.id:
            raise HTTPException(404, "This exercise is unavailable")
        return bool(data["solved"])
    except (httpx.HTTPError, ValueError, TypeError):
        raise HTTPException(503, "The exercise could not be checked") from None


async def get_room(unit_id: str, user: User, token: str, course_id: str | None = None) -> RoomEnvelope:
    content = catalogue()
    states = await read_states(user.id, {unit_id}) if course_id is not None else await read_states(user.id)
    unit = find_unit(content, unit_id, states, prerequisites=course_id is None)
    await require_path_access(content, unit.path_id, user, course_id)
    await challenge_status(unit, user, token)
    row = states.get(unit.id)
    return RoomEnvelope(
        unit=await public_unit(unit, user, course_id),
        progress=progress(row),
        review_available=course_id is not None and review_available(row),
    )


def review_available(row: RoomState | None) -> bool:
    return row is not None and row.status in ("completed", "skipped") and row.review_status != "in_progress"


async def next_room(
    user: User,
    token: str,
    path_id: str,
    after: str | None,
    continuous: bool = False,
    direction: str | None = None,
    course_id: str | None = None,
    unit_id: str | None = None,
) -> Rooms:
    content = catalogue()
    path = next((path for path in content.paths if path.id == path_id), None)
    if path is None or (after is not None and after not in path.units):
        raise HTTPException(404, "This learning path is unavailable")
    if direction is not None and path.direction_id != direction:
        raise HTTPException(404, "This learning direction is unavailable")
    if unit_id is not None and (course_id is None or after is not None):
        raise HTTPException(422, "Choose a course lesson or continue after a lesson")
    states = (
        await read_states(user.id, {unit_id} if unit_id is not None else set(path.units))
        if course_id is not None
        else await read_states(user.id)
    )
    if course_id is not None:
        await require_path_access(content, path.id, user, course_id)
    # Unknown legacy directions retain their historical cross-path stream. An
    # explicit direction stays in its own subject, including when it repeats.
    scope = direction or (path.direction_id if continuous else None)
    accessible = await accessible_paths(content, user)
    if path.id not in accessible:
        raise HTTPException(403, "Open the course to get access to these lessons")
    content.paths = [candidate for candidate in content.paths if candidate.id in accessible]
    choices = [LearningPath.parse_obj(candidate.dict(exclude={"units"})) for candidate in content.paths]
    if course_id is not None:
        if unit_id is not None:
            if unit_id not in path.units:
                raise HTTPException(404, "This lesson is not part of that course")
            chosen = await get_room(unit_id, user, token, course_id)
            return Rooms(paths=choices, path=LearningPath.parse_obj(path.dict(exclude={"units"})), next=chosen)
        return await next_course_room(content, states, user, token, path, after, continuous, choices, course_id)
    content.paths = [candidate for candidate in content.paths if scope is None or candidate.direction_id == scope]
    if continuous:
        return await continuous_room(content, states, user, token, path_id, after, choices)
    start = path.units.index(after) + 1 if after is not None else 0
    ids = path.units[start:]
    if after is None:
        # The curated order breaks ties between unfinished working states.
        ids = sorted(ids, key=lambda uid: 0 if uid in states and states[uid].status == "in_progress" else 1)
    selected = None
    blocked_by_prerequisite = False
    for unit_id in ids:
        if unit_id in states and states[unit_id].status in ("completed", "skipped"):
            continue
        try:
            unit = find_unit(content, unit_id, states)
            await challenge_status(unit, user, token)
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                blocked_by_prerequisite = blocked_by_prerequisite or exc.status_code == 403
                continue
            raise
        selected = RoomEnvelope(unit=await public_unit(unit, user), progress=progress(states.get(unit.id)))
        break
    active = [unit for unit in content.units if unit.path_id == path.id and not unit.retired]
    finished = bool(active) and all(
        unit.id in states and states[unit.id].status in ("completed", "skipped") for unit in active
    )
    reason: Literal["completed", "unavailable", "prerequisites"] | None = None
    if selected is None:
        reason = "completed" if finished else "prerequisites" if blocked_by_prerequisite else "unavailable"
    return Rooms(
        paths=choices, path=LearningPath.parse_obj(path.dict(exclude={"units"})), next=selected, empty_reason=reason
    )


async def next_course_room(
    content: Catalogue,
    states: dict[str, RoomState],
    user: User,
    token: str,
    path: CataloguePath,
    after: str | None,
    continuous: bool,
    choices: list[LearningPath],
    course_id: str | None = None,
) -> Rooms:
    """Follow an explicit course choice without inventing earlier achievements."""
    ids = path.units
    if after is not None:
        cursor = ids.index(after) + 1
        ids = ids[cursor:] + (ids[:cursor] if continuous else [])
    else:
        # Returning to a course resumes the latest own working state, including
        # a repeat. An explicit next click instead follows the declared order.
        def position(uid: str) -> tuple[int, float]:
            row = states.get(uid)
            if row is not None and (
                row.status == "in_progress" or (row.review_id is not None and row.review_status == "in_progress")
            ):
                return (0, -row.updated_at.timestamp())
            return (2 if row is not None and row.status in ("completed", "skipped") else 1, ids.index(uid))

        ids = sorted(ids, key=position)
    selected = None
    for uid in ids:
        row = states.get(uid)
        if not continuous and review_available(row):
            continue
        try:
            unit = find_unit(content, uid, states, prerequisites=False)
            await challenge_status(unit, user, token)
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        selected = RoomEnvelope(
            unit=await public_unit(unit, user, course_id),
            progress=progress(row),
            review_available=review_available(row),
        )
        break
    return Rooms(
        paths=choices,
        path=LearningPath.parse_obj(path.dict(exclude={"units"})),
        next=selected,
        empty_reason=(
            None if selected is not None else "completed" if path_completed(content, states, path.id) else "unavailable"
        ),
    )


def path_completed(content: Catalogue, states: dict[str, RoomState], path_id: str) -> bool:
    active = [unit for unit in content.units if unit.path_id == path_id and not unit.retired]
    return bool(active) and all(
        unit.id in states and states[unit.id].status in ("completed", "skipped") for unit in active
    )


async def course_completions(user: User | None) -> dict[str, bool]:
    if user is None or not settings.rooms_enabled:
        return {}
    path_ids = {course.learning_path_id for course in COURSES.values() if course.learning_path_id is not None}
    if not path_ids:
        return {}
    content = catalogue()
    states = await read_states(user.id)
    return {path.id: path_completed(content, states, path.id) for path in content.paths if path.id in path_ids}


async def course_learning(course: Course, user: User, token: str) -> CourseLearning:
    content = catalogue()
    path = next((path for path in content.paths if path.id == course.learning_path_id), None)
    if path is None:
        raise HTTPException(404, "This course does not have a learning path yet")
    await require_path_access(content, path.id, user)
    states = await read_states(user.id)
    units = {unit.id: unit for unit in content.units if not unit.retired}
    outline = []
    for uid in path.units:
        if uid not in units:
            continue
        unit = units[uid]
        try:
            find_unit(content, uid, states)
            available = True
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            available = False
        row = states.get(uid)
        outline.append(
            CourseLearningUnit.parse_obj(
                {
                    "id": unit.id,
                    "chapter_id": unit.chapter_id,
                    "title": unit.title,
                    "room": unit.room,
                    "status": "new" if row is None else row.status,
                    "result": None if row is None else row.result,
                    "available": available,
                    "selectable": unit.completion is not None or unit.exercise is not None,
                }
            )
        )
    selected = await next_room(user, token, path.id, None, course_id=course.id)
    return CourseLearning(
        path=selected.path,
        units=outline,
        next=selected.next,
        completed=path_completed(content, states, path.id),
        empty_reason=selected.empty_reason,
    )


async def continuous_room(
    content: Catalogue,
    states: dict[str, RoomState],
    user: User,
    token: str,
    path_id: str,
    after: str | None,
    choices: list[LearningPath],
) -> Rooms:
    paths = content.paths
    index = next(i for i, path in enumerate(paths) if path.id == path_id)
    ordered_paths = paths[index:] + paths[:index]
    all_ids = [uid for path in ordered_paths for uid in path.units]
    cursor = all_ids.index(after) + 1 if after is not None else 0
    review_ids = all_ids[cursor:] + all_ids[:cursor]
    blocked = False

    async def available(uid: str) -> CatalogueUnit | None:
        nonlocal blocked
        try:
            unit = find_unit(content, uid, states)
            await challenge_status(unit, user, token)
            return unit
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            blocked = blocked or exc.status_code == 403
            return None

    async def response(unit: CatalogueUnit, start_review: bool = False) -> Rooms:
        selected_path = next(path for path in paths if path.id == unit.path_id)
        return Rooms(
            paths=choices,
            path=LearningPath.parse_obj(selected_path.dict(exclude={"units"})),
            next=RoomEnvelope(
                unit=await public_unit(unit, user),
                progress=progress(states.get(unit.id)),
                review_available=start_review,
            ),
        )

    # Returning to the character sheet must not discard unfinished work in a
    # different chapter of the same direction. Review state is private work too.
    for uid in all_ids:
        row = states.get(uid)
        if uid == after or row is None:
            continue
        if row.status == "in_progress" or (row.review_id is not None and row.review_status == "in_progress"):
            if unit := await available(uid):
                return await response(unit)

    # Finish new learning before repeating completed material. A path boundary
    # never sends the learner out of the stream; old GET callers retain their API.
    for path in ordered_paths:
        ids = path.units
        if path.id == path_id and after is not None:
            split = ids.index(after)
            following = split + 1
            ids = ids[following:] + ids[:split]
        elif after is None:
            ids = sorted(ids, key=lambda uid: 0 if uid in states and states[uid].status == "in_progress" else 1)
        for uid in ids:
            if uid in states and states[uid].status in ("completed", "skipped"):
                continue
            if unit := await available(uid):
                return await response(unit)
    # Resume private repeat work, then cycle through available completed rooms.
    # The cursor moves across paths and never implies a fixed type alternation.
    for resume in (True, False):
        for uid in review_ids:
            row = states.get(uid)
            if row is None or row.status not in ("completed", "skipped"):
                continue
            active = row.review_id is not None and row.review_status == "in_progress"
            if active != resume or (active and uid == after):
                continue
            if unit := await available(uid):
                return await response(unit, start_review=not active)
    # A one-room stream can still resume its only skipped working state.
    if after is not None and (unit := await available(after)):
        row = states.get(after)
        return await response(
            unit,
            start_review=row is not None
            and row.status in ("completed", "skipped")
            and row.review_status != "in_progress",
        )
    return Rooms(
        paths=choices,
        path=LearningPath.parse_obj(ordered_paths[0].dict(exclude={"units"})),
        next=None,
        empty_reason="prerequisites" if blocked else "unavailable",
    )


async def review_attempt_solved(unit: CatalogueUnit, user: User, token: str, row: RoomState, attempt_id: UUID) -> bool:
    exercise = unit.exercise
    if exercise is None or row.review_started_at is None:
        return False
    resource = {"multiple_choice": "multiple_choice", "matching": "matchings", "coding": "coding_challenges"}[
        exercise.type
    ]
    path = f"/tasks/{exercise.task_id}/{resource}/{exercise.subtask_id}"
    coding = exercise.type == "coding"
    path += "/submissions" if coding else f"/attempts/{attempt_id}"
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                settings.challenges_url.rstrip("/") + path, headers={"Authorization": f"Bearer {token}"}
            )
        if response.status_code == 401:
            raise HTTPException(401, "Please sign in again")
        if response.status_code in (403, 404):
            return False
        if response.status_code != 200:
            raise HTTPException(503, "The attempt could not be checked")
        data = response.json()
        if coding:
            if not isinstance(data, list):
                raise HTTPException(503, "The attempt could not be checked")
            data = next((item for item in data if isinstance(item, dict) and item.get("id") == str(attempt_id)), None)
        if not isinstance(data, dict) or data.get("id") != str(attempt_id):
            return False
        if data.get("subtask_id") != str(exercise.subtask_id):
            return False
        if data.get("creator" if coding else "user_id") != user.id:
            return False
        if not coding and data.get("task_id") != str(exercise.task_id):
            return False
        timestamp = datetime.fromisoformat(
            data["creation_timestamp" if coding else "created_at"].replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None:
            return False
        started = row.review_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if timestamp < started:
            return False
        if coding:
            return isinstance(data.get("result"), dict) and data["result"].get("verdict") == "OK"
        return data.get("solved") is True
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, "The attempt could not be checked") from None


def request_fingerprint(unit_id: str, data: SaveState | Complete | StartReview, course_id: str | None = None) -> str:
    operation = "save" if isinstance(data, SaveState) else "review" if isinstance(data, StartReview) else "complete"
    payload = {"unit_id": unit_id, "operation": operation}
    if course_id is not None:
        payload["course_id"] = course_id
    return sha256(
        json.dumps(
            {**payload, **json.loads(data.json(exclude_none=True))}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def completed_progress(unit: CatalogueUnit, current: Progress, data: Complete, solved: bool) -> Progress:
    if current.status in ("completed", "skipped"):
        raise HTTPException(409, "This room has already been finished")
    if data.action == "skip":
        if unit.completion is None or not unit.completion.allow_skip:
            raise HTTPException(403, "Only introductions can be skipped")
        return current.copy(update={"status": "skipped", "result": None})
    if unit.exercise is not None:
        if not solved:
            raise HTTPException(409, "The exercise has not been solved yet")
        return current.copy(update={"status": "completed", "result": Result(kind="solved")})
    # Canonical JSON distinguishes true from 1; the client cannot assert mastery.
    if unit.completion is None or json.dumps(data.answer, sort_keys=True) != json.dumps(
        unit.completion.answer, sort_keys=True
    ):
        raise HTTPException(422, "Check your answer and try again")
    return current.copy(update={"status": "completed", "result": Result(kind="introduced")})


async def mutate_room(
    unit_id: str, user: User, token: str, data: SaveState | Complete | StartReview, course_id: str | None = None
) -> RoomEnvelope:
    content = catalogue()
    fingerprint = request_fingerprint(unit_id, data, course_id)
    # This is also the erasure lock. A request admitted before erasure must not
    # recreate a state or replay receipt after the tombstone has committed.
    guard = await lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    states = await read_states(user.id, {unit_id}) if course_id is not None else await read_states(user.id)
    unit = find_unit(content, unit_id, states, prerequisites=course_id is None)
    await require_path_access(content, unit.path_id, user, course_id)
    receipt = await db.get(RoomRequest, user_id=user.id, request_id=str(data.request_id))
    if receipt is not None:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, "This request was already used for another change")
        return RoomEnvelope(
            unit=await public_unit(unit, user, course_id), progress=Progress.parse_obj(receipt.progress)
        )
    row = states.get(unit_id)
    current = progress(row)
    if current.revision != data.expected_revision:
        raise HTTPException(409, "Your room has changed in another session")
    if not isinstance(data, StartReview) and data.review_id != current.review_id:
        raise HTTPException(409, "Your practice round has changed in another session")
    solved = await challenge_status(unit, user, token)
    if isinstance(data, StartReview):
        if row is None or row.status not in ("completed", "skipped") or row.review_status == "in_progress":
            raise HTTPException(409, "This room is already in progress")
        current = Progress(revision=current.revision, status="in_progress", review_id=data.request_id)
    elif isinstance(data, SaveState):
        current.state = data.state
        if current.status == "new":
            current.status = "in_progress"
    else:
        if current.review_id is not None and unit.exercise is not None:
            solved = (
                row is not None
                and data.attempt_id is not None
                and await review_attempt_solved(unit, user, token, row, data.attempt_id)
            )
        current = completed_progress(unit, current, data, solved)
    current.revision += 1
    values: dict[str, Any] = {**json.loads(current.json()), "updated_at": utcnow()}
    if current.review_id is not None and row is not None:
        values.update(status=row.status, result=row.result, review_status=current.status)
        if isinstance(data, StartReview):
            values["review_started_at"] = utcnow()
    if row is None:
        await db.add(RoomState(user_id=user.id, unit_id=unit_id, **values))
    else:
        result = await db.exec(
            update(RoomState)
            .where(
                RoomState.user_id == user.id, RoomState.unit_id == unit_id, RoomState.revision == data.expected_revision
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise HTTPException(409, "Your room has changed in another session")
    await db.add(
        RoomRequest(
            user_id=user.id,
            request_id=str(data.request_id),
            unit_id=unit_id,
            revision=current.revision,
            fingerprint=fingerprint,
            progress=json.loads(current.json()),
            created_at=utcnow(),
        )
    )
    try:
        await db.session.flush()
    except IntegrityError:
        # The normal PostgreSQL/MySQL user lock serializes creation too. The
        # unique key additionally protects databases without row-level locks.
        await db.session.rollback()
        raise HTTPException(409, "Your room has changed in another session") from None
    # Autosaves contain private drafts. Keep only the four newest exact retries,
    # using revisions rather than wall-clock order; older retries still fail CAS.
    keep = await db.all(
        filter_by(RoomRequest, user_id=user.id, unit_id=unit_id).order_by(RoomRequest.revision.desc()).limit(4)
    )
    await db.exec(
        delete(RoomRequest).where(
            RoomRequest.user_id == user.id,
            RoomRequest.unit_id == unit_id,
            RoomRequest.request_id.notin_([receipt.request_id for receipt in keep]),
        )
    )
    return RoomEnvelope(unit=await public_unit(unit, user, course_id), progress=current)
