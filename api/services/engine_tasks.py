"""A learner's tasks in a unit of the lesson engine: start, submission and reading a submission (§9, Ä23, Ä37).

Start and submission run under the user guard (`lock_user`, also the erasure lock) and the row lock of the task
state, like the room routes. Evaluation in the sandbox and booking of the verdict follow in the next change; until
then a submission stays pending.
"""

from hashlib import sha256
from secrets import token_hex
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from api.database import db, filter_by
from api.exceptions.engine import EngineRefusal
from api.models import EngineRevision, EngineSubmission, EngineTaskState, RoomState
from api.schemas.engine import StartRequest, SubmitRequest
from api.schemas.rooms import CatalogueUnit
from api.schemas.user import User
from api.services import daily_limit, rooms
from api.services.engine import is_engine_unit
from api.services.purchases import lock_user
from api.utils.canonical import CanonicalError, canonical
from api.utils.utc import utcnow

NOT_FOUND = "This task is not available"


async def _guarded(user: User) -> None:
    guard = await lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This account is no longer available")


async def _unit(unit_id: str, user: User, course_id: str | None) -> CatalogueUnit:
    content = rooms.catalogue()
    states = await rooms.read_states(user.id, {unit_id}) if course_id is not None else await rooms.read_states(user.id)
    try:
        unit = rooms.find_unit(content, unit_id, states, prerequisites=course_id is None)
    except HTTPException as error:
        if error.status_code == 404:
            raise EngineRefusal(404, "not_found", NOT_FOUND) from None
        raise
    if not is_engine_unit(unit):
        raise EngineRefusal(404, "not_found", NOT_FOUND)
    await rooms.require_path_access(content, unit.path_id, user, course_id, unit.id)
    return unit


async def _revision(unit: CatalogueUnit) -> EngineRevision:
    revision: EngineRevision | None = await db.first(filter_by(EngineRevision, active_for=unit.id))
    if revision is None:
        # The catalogue offers the unit only with an active revision; it was replaced during this request.
        raise EngineRefusal(503, "unavailable", "This lesson is being updated; try again", retryable=True)
    return revision


def _task(revision: EngineRevision, goal_id: str) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = revision.server_document["tasks"]
    task = next((item for item in tasks if item["goalId"] == goal_id), None)
    if task is None:
        raise EngineRefusal(404, "not_found", NOT_FOUND)
    return task


def view(submission: EngineSubmission, completed: bool) -> dict[str, Any]:
    """The protocol's AssessmentView of a submission; XP stand only in the answer that booked them (Ä28)."""
    if submission.state == "accepted":
        return {"assessmentId": submission.id, "goalId": submission.goal_id, "status": "pending"}
    return {
        "assessmentId": submission.id,
        "goalId": submission.goal_id,
        "status": "final",
        "outcome": submission.verdict,
        "counts": submission.counts,
        "feedback": submission.feedback,
        **({"completed": True} if completed and submission.counts else {}),
    }


async def _completed(user_id: str, unit_id: str) -> bool:
    room = await db.get(RoomState, user_id=user_id, unit_id=unit_id)
    return room is not None and room.status == "completed"


async def unit_assessments(user_id: str, unit_id: str) -> list[dict[str, Any]]:
    """Ä23: the pending submissions of the whole unit and the counted pass of each task, so a reopened unit knows
    every task; a pass says whether the unit is completed."""
    rows = await db.all(filter_by(EngineSubmission, user_id=user_id, unit_id=unit_id))
    completed = await _completed(user_id, unit_id)
    pending = [view(row, completed) for row in sorted(rows, key=lambda row: row.accepted_at) if row.state == "accepted"]
    passes: dict[str, EngineSubmission] = {}
    for row in sorted(rows, key=lambda row: row.booked_at or row.accepted_at):
        if row.state == "booked" and row.counts:
            passes[row.goal_id] = row
    return [*pending, *(view(row, completed) for row in passes.values())]


def _public_start(revision: EngineRevision, task: dict[str, Any], band: str) -> dict[str, Any]:
    practice = task["practice"][band]
    return {
        "startId": str(uuid4()),
        "revisionId": revision.id,
        "goalId": task["goalId"],
        "caseId": practice["caseId"],
        "config": practice["config"],
        "initial": practice["initial"],
        # Drawn once by the server; 128 bits as 32 lowercase hex digits (§8).
        "seed": token_hex(16),
    }


async def start(unit_id: str, goal_id: str, data: StartRequest, user: User) -> dict[str, Any]:
    await _guarded(user)
    unit = await _unit(unit_id, user, data.courseId)
    revision = await _revision(unit)
    if data.revisionId != revision.id:
        # The frame runs bytes of another revision; only loading the unit again helps (Ä32b).
        raise EngineRefusal(409, "revision_changed", "This lesson has a new version; open it again")
    task = _task(revision, goal_id)
    state: EngineTaskState | None = await db.first(
        filter_by(EngineTaskState, user_id=user.id, unit_id=unit.id, goal_id=goal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    pending = await db.first(
        filter_by(EngineSubmission, user_id=user.id, unit_id=unit.id, goal_id=goal_id, state="accepted")
    )
    replay = state is not None and state.start is not None and state.start_request_id == data.requestId
    if pending is not None and not replay and state is not None and state.revision_id != revision.id:
        # A pending submission keeps its start; the frame waits for the verdict first.
        raise EngineRefusal(409, "pending", "A submission of this task is still being checked", retryable=True)

    if state is None:
        state = EngineTaskState(
            user_id=user.id,
            unit_id=unit.id,
            goal_id=goal_id,
            revision_id=revision.id,
            band=revision.server_document["bandRule"]["initial"],
            fails=0,
            passes=0,
            xp_applied=False,
            updated_at=utcnow(),
        )
        await db.add(state)
    elif not replay and pending is None and state.revision_id != revision.id:
        # §9: a start on the newer revision; the saved plan stays, the band too (both revisions have the same bands).
        state.revision_id = revision.id
        state.start_id = state.start = state.start_band = None

    if state.start is None:
        public = _public_start(revision, task, state.band)
        state.start_id, state.start, state.start_band = public["startId"], public, state.band
        state.start_request_id = data.requestId
    state.updated_at = utcnow()
    await daily_limit.start_unit(user, unit.id, data.courseId, locked=True)
    await db.session.flush()
    return {"start": state.start, "assessments": await unit_assessments(user.id, unit.id)}


async def submit(unit_id: str, goal_id: str, data: SubmitRequest, user: User) -> tuple[dict[str, Any], int]:
    await _guarded(user)
    unit = await _unit(unit_id, user, data.courseId)
    plan = data.answer.input.model_dump(mode="json")
    try:
        input_hash = sha256(canonical(plan)).hexdigest()
    except CanonicalError:
        raise EngineRefusal(422, "plan_invalid", "The plan holds values that cannot be submitted") from None
    state = await db.first(
        filter_by(EngineTaskState, user_id=user.id, unit_id=unit.id, goal_id=goal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if state is None or state.start_id is None:
        raise EngineRefusal(409, "start_required", "Start the task first")

    # §9: the same plan for the same start is the same submission, whatever request carries it; checked first.
    same = await db.first(
        filter_by(
            EngineSubmission,
            user_id=user.id,
            unit_id=unit.id,
            goal_id=goal_id,
            start_id=data.answer.startId,
            input_hash=input_hash,
        )
    )
    if same is not None:
        return view(same, await _completed(user.id, unit.id)), 202 if same.state == "accepted" else 200
    if state.start_id != data.answer.startId:
        raise EngineRefusal(409, "start_required", "This start is used up; start the task again")
    taken = await db.first(
        filter_by(EngineSubmission, user_id=user.id, unit_id=unit.id, goal_id=goal_id, start_id=state.start_id)
    )
    if taken is not None:
        # Ä37: a start carries one plan; another plan needs the next start.
        raise EngineRefusal(409, "start_required", "This start is used up; start the task again")

    # The submitted plan is the confirmed one: the room has this revision and holds exactly this plan (§9).
    room = await db.get(RoomState, user_id=user.id, unit_id=unit.id)
    plans = room.state.get("plans") if room is not None and isinstance(room.state, dict) else None
    saved = plans.get(goal_id) if isinstance(plans, dict) else None
    if room is None or room.revision != data.stateRevision or saved is None or not _same(saved, plan):
        raise EngineRefusal(409, "conflict", "The saved plan has changed; submit the plan that is saved")

    revision = await db.get(EngineRevision, id=state.revision_id)
    if revision is None:
        raise EngineRefusal(503, "unavailable", "This lesson is being updated; try again", retryable=True)
    row = EngineSubmission(
        id=str(uuid4()),
        user_id=user.id,
        unit_id=unit.id,
        goal_id=goal_id,
        open_goal_id=goal_id,
        revision_id=state.revision_id,
        start_id=state.start_id,
        input_hash=input_hash,
        input=plan,
        # Frozen now: only these decide the verdict (§9).
        job={
            "revisionId": revision.id,
            "publicStart": state.start,
            "band": state.start_band,
            "serverSha256": revision.server_sha256,
        },
        state="accepted",
        accepted_at=utcnow(),
    )
    await db.add(row)
    try:
        await db.session.flush()
    except IntegrityError:
        # Another submission of this task got in first; the next read finds it.
        await db.session.rollback()
        raise EngineRefusal(409, "conflict", "Another submission of this task came first", retryable=True) from None
    return view(row, False), 202


def _same(saved: Any, plan: dict[str, Any]) -> bool:
    try:
        return canonical(saved) == canonical(plan)
    except CanonicalError:
        return False


async def status(assessment_id: str, user: User) -> dict[str, Any]:
    row = await db.first(filter_by(EngineSubmission, id=assessment_id, user_id=user.id))
    if row is None:
        raise EngineRefusal(404, "not_found", "There is no such submission")
    return view(row, await _completed(user.id, row.unit_id))
