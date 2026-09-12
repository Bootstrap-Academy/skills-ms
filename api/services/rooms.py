"""Curated learning flow with private state; never awards XP or changes courses."""

import json
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

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
    return Progress.parse_obj(
        {"revision": row.revision, "state": row.state, "status": row.status, "result": row.result}
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


async def next_room(user: User, token: str, path_id: str, after: str | None) -> Rooms:
    content = catalogue()
    states = await read_states(user.id)
    path = next((path for path in content.paths if path.id == path_id), None)
    if path is None or (after is not None and after not in path.units):
        raise HTTPException(404, "This learning path is unavailable")
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


def request_fingerprint(unit_id: str, data: SaveState | Complete) -> str:
    payload = {"unit_id": unit_id, "operation": "save" if isinstance(data, SaveState) else "complete"}
    return sha256(
        json.dumps({**payload, **json.loads(data.json())}, sort_keys=True, separators=(",", ":")).encode()
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


async def mutate_room(unit_id: str, user: User, token: str, data: SaveState | Complete) -> RoomEnvelope:
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
    solved = await challenge_status(unit, user, token)
    if isinstance(data, SaveState):
        current.state = data.state
        if current.status == "new":
            current.status = "in_progress"
    else:
        current = completed_progress(unit, current, data, solved)
    current.revision += 1
    values: dict[str, Any] = {**current.dict(), "updated_at": utcnow()}
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
            progress=current.dict(),
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
