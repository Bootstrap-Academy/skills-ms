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
from api.schemas.rooms import (
    Catalogue,
    CatalogueUnit,
    Complete,
    Exercise,
    LearningPath,
    Progress,
    Result,
    RoomEnvelope,
    Rooms,
    SaveState,
    StartReview,
)
from api.schemas.user import User
from api.services.purchases import lock_user
from api.settings import settings
from api.utils.utc import utcnow


@lru_cache(maxsize=1)
def load_catalogue() -> Catalogue:
    try:
        return Catalogue.parse_raw(Path(__file__).parents[1].joinpath("content/learning_rooms.json").read_text())
    except (OSError, ValueError, ValidationError):
        raise HTTPException(503, "Learning rooms are temporarily unavailable") from None


def catalogue() -> Catalogue:
    if not settings.rooms_enabled:
        raise HTTPException(404, "Learning rooms are unavailable")
    content = load_catalogue().copy(deep=True)
    by_id = {unit.id: unit for unit in content.units}
    try:
        for unit_id, reference in settings.learning_rooms_exercise_refs.items():
            if unit_id not in by_id or by_id[unit_id].room != "exercise":
                raise ValueError("Unknown exercise mapping")
            by_id[unit_id].exercise = Exercise.parse_obj(reference)
    except (ValueError, ValidationError):
        raise HTTPException(503, "Learning-room exercises are temporarily unavailable") from None
    return content


async def read_states(user_id: str) -> dict[str, RoomState]:
    # Reads never create the durable user lock or a working-state row.
    guard = await db.get(PurchaseUser, user_id=user_id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    return {
        row.unit_id: row
        for row in await db.all(filter_by(RoomState, user_id=user_id).execution_options(populate_existing=True))
    }


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


def find_unit(content: Catalogue, unit_id: str, states: dict[str, RoomState]) -> CatalogueUnit:
    unit = next((unit for unit in content.units if unit.id == unit_id and not unit.retired), None)
    if unit is None or (unit.room == "exercise" and unit.exercise is None):
        raise HTTPException(404, "This learning room is unavailable")
    if not set(unit.requires).issubset(introduced_concepts(content, states)):
        raise HTTPException(403, "Start with the introduction for this room")
    return unit


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


async def get_room(unit_id: str, user: User, token: str) -> RoomEnvelope:
    content = catalogue()
    states = await read_states(user.id)
    unit = find_unit(content, unit_id, states)
    await challenge_status(unit, user, token)
    return RoomEnvelope(unit=unit.public(), progress=progress(states.get(unit.id)))


async def next_room(user: User, token: str, path_id: str, after: str | None, continuous: bool = False) -> Rooms:
    content = catalogue()
    states = await read_states(user.id)
    path = next((path for path in content.paths if path.id == path_id), None)
    if path is None or (after is not None and after not in path.units):
        raise HTTPException(404, "This learning path is unavailable")
    if continuous:
        return await continuous_room(content, states, user, token, path_id, after)
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
        selected = RoomEnvelope(unit=unit.public(), progress=progress(states.get(unit.id)))
        break
    active = [unit for unit in content.units if unit.path_id == path.id and not unit.retired]
    finished = bool(active) and all(
        unit.id in states and states[unit.id].status in ("completed", "skipped") for unit in active
    )
    reason: Literal["completed", "unavailable", "prerequisites"] | None = None
    if selected is None:
        reason = "completed" if finished else "prerequisites" if blocked_by_prerequisite else "unavailable"
    return Rooms(
        paths=[LearningPath.parse_obj(path.dict(exclude={"units"})) for path in content.paths],
        path=LearningPath.parse_obj(path.dict(exclude={"units"})),
        next=selected,
        empty_reason=reason,
    )


async def continuous_room(
    content: Catalogue, states: dict[str, RoomState], user: User, token: str, path_id: str, after: str | None
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

    def response(unit: CatalogueUnit, start_review: bool = False) -> Rooms:
        selected_path = next(path for path in paths if path.id == unit.path_id)
        return Rooms(
            paths=[LearningPath.parse_obj(path.dict(exclude={"units"})) for path in paths],
            path=LearningPath.parse_obj(selected_path.dict(exclude={"units"})),
            next=RoomEnvelope(
                unit=unit.public(), progress=progress(states.get(unit.id)), review_available=start_review
            ),
        )

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
                return response(unit)
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
                return response(unit, start_review=not active)
    # A one-room stream can still resume its only skipped working state.
    if after is not None and (unit := await available(after)):
        row = states.get(after)
        return response(
            unit,
            start_review=row is not None
            and row.status in ("completed", "skipped")
            and row.review_status != "in_progress",
        )
    return Rooms(
        paths=[LearningPath.parse_obj(path.dict(exclude={"units"})) for path in paths],
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


def request_fingerprint(unit_id: str, data: SaveState | Complete | StartReview) -> str:
    operation = "save" if isinstance(data, SaveState) else "review" if isinstance(data, StartReview) else "complete"
    payload = {"unit_id": unit_id, "operation": operation}
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


async def mutate_room(unit_id: str, user: User, token: str, data: SaveState | Complete | StartReview) -> RoomEnvelope:
    content = catalogue()
    fingerprint = request_fingerprint(unit_id, data)
    # This is also the erasure lock. A request admitted before erasure must not
    # recreate a state or replay receipt after the tombstone has committed.
    guard = await lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    states = await read_states(user.id)
    unit = find_unit(content, unit_id, states)
    receipt = await db.get(RoomRequest, user_id=user.id, request_id=str(data.request_id))
    if receipt is not None:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, "This request was already used for another change")
        return RoomEnvelope(unit=unit.public(), progress=Progress.parse_obj(receipt.progress))
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
    return RoomEnvelope(unit=unit.public(), progress=current)
