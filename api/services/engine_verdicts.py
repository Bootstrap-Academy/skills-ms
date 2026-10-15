"""Verdicts of the lesson engine: the view of a submission and the booking of its evaluation (§9, §11, Ä26, Ä28).

A result of the runner (Ä26) is booked once, in one transaction, in the order of §9: for a pass `apply_xp` first, so
that its operation lock comes before every other lock; then the user guard (also the erasure lock), the task state
and the submission, each locked. Only an accepted submission is booked; a booked one is only read. A booking counts
the verdict for the band (§11), uses up the start, records the first pass with its XP and, once every required task
has passed, completes the room in the same transaction. It does not raise the room's revision: only saves of the
plan do (§9), and a later save keeps the fields because it reads them from the row under the same guard. Any
failure rolls the whole booking back and leaves the submission accepted.
"""

from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import update
from sqlalchemy.engine import CursorResult

from api.database import db, filter_by, select
from api.models import EngineRevision, EngineSubmission, EngineTaskState, RoomState
from api.services.benefits import XPAward, apply_xp
from api.services.purchases import lock_user
from api.services.sandbox import SandboxUnavailableError
from api.utils.canonical import canonical
from api.utils.utc import utcnow

# §9: the XP of a task have one identity per learner, unit and task, whatever revision or submission earns them.
XP_NAMESPACE = UUID("4666e0ec-8a60-5ca8-8914-7d61fd6dc388")
SUMMARY = {"passed": "Dein Plan besteht alle Fälle.", "not_passed": "Dein Plan besteht noch nicht alle Fälle."}


class ErasedError(Exception):
    """The learner's data was erased; nothing is booked."""


def xp_operation_id(user_id: str, unit_id: str, goal_id: str) -> str:
    """The operation of `apply_xp` and the earning id: UUIDv5 of the canonical name `[userId, unitId, goalId, "xp"]`."""
    return str(uuid5(XP_NAMESPACE, canonical([user_id, unit_id, goal_id, "xp"]).decode("utf-8")))


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


async def room_completed(user_id: str, unit_id: str) -> bool:
    # A booking in this session may have completed the room with an UPDATE; read the row, not the identity map.
    room = await db.first(
        filter_by(RoomState, user_id=user_id, unit_id=unit_id).execution_options(populate_existing=True)
    )
    return room is not None and room.status == "completed"


def bound(result: dict[str, Any], task: dict[str, Any]) -> bool:
    """Whether a result fits the task it was evaluated for: every declared criterion exactly once, only feedback
    codes the task holds, and a pass only with every criterion met. Anything else is technical, never a verdict."""
    ids = [item["id"] for item in result["criteria"]]
    return (
        len(ids) == len(set(ids))
        and set(ids) == set(task["criteria"])
        and set(result["feedbackCodes"]) <= set(task["feedback"])
        and (result["verdict"] == "not_passed" or all(item["passed"] for item in result["criteria"]))
    )


def _text(value: dict[str, str]) -> str:
    # The lessons of stage 1 speak German; a text in another language only stands in.
    return value.get("de") or next(iter(value.values()), "") or ""


def feedback(result: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    """The feedback a learner may see: general texts of the package for the codes of a fail, each once (§9)."""
    passed = result["verdict"] == "passed"
    codes = [] if passed else list(dict.fromkeys(result["feedbackCodes"]))
    reasons = [text for code in codes if (text := _text(task["feedback"][code]))]
    return {"source": "server", "summary": SUMMARY[result["verdict"]], "public_reasons": reasons}


def counted(rule: dict[str, Any], band: str, fails: int, passes: int, passed: bool) -> tuple[str, int, int]:
    """§11: a booked verdict counts once. At its threshold the band moves one position (at the edge it stays) and
    both counters start again."""
    bands: list[str] = rule["bands"]
    at = bands.index(band)
    if passed:
        passes, fails = passes + 1, 0
        if passes >= rule["raiseAfter"]:
            return bands[min(len(bands) - 1, at + 1)], 0, 0
    else:
        fails, passes = fails + 1, 0
        if fails >= rule["lowerAfter"]:
            return bands[max(0, at - 1)], 0, 0
    return band, fails, passes


async def _complete_unit(user_id: str, unit_id: str, tasks: list[dict[str, Any]]) -> bool:
    """Completes the room once every required task has a booked pass; whether the unit is completed."""
    required = {task["goalId"] for task in tasks if task["required"]}
    passed = set(
        await db.all(
            select(EngineTaskState.goal_id).where(
                EngineTaskState.user_id == user_id,
                EngineTaskState.unit_id == unit_id,
                EngineTaskState.passed_at.is_not(None),
            )
        )
    )
    if not required <= passed:
        return False
    done = await db.exec(
        update(RoomState)
        .where(RoomState.user_id == user_id, RoomState.unit_id == unit_id, RoomState.status != "completed")
        # Only progress fields; the revision stays (§9).
        .values(status="completed", result={"kind": "passed"}, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    return cast(CursorResult, done).rowcount == 1 or await room_completed(user_id, unit_id)


async def book(submission_id: str, user_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Books a result of the runner for an accepted submission and answers its view; `xp` only when this booking
    earned them. A submission booked by another evaluation is only read. Raises ErasedError when the learner was
    erased and SandboxUnavailableError when the result does not fit the task."""
    row: EngineSubmission | None = await db.first(filter_by(EngineSubmission, id=submission_id, user_id=user_id))
    if row is None:
        raise ErasedError
    unit_id, goal_id = row.unit_id, row.goal_id
    if row.state != "accepted":
        return view(row, await room_completed(user_id, unit_id))
    revision = cast(EngineRevision, await db.get(EngineRevision, id=row.revision_id))
    document = revision.server_document
    task = next(item for item in document["tasks"] if item["goalId"] == goal_id)
    terms = next(item for item in revision.tasks if item["goalId"] == goal_id)
    if not bound(result, task):
        raise SandboxUnavailableError("the result does not fit the task")
    passed = result["verdict"] == "passed"
    operation = xp_operation_id(user_id, unit_id, goal_id)

    if passed:
        # §9: the operation lock of the XP before every other lock. Idempotent: a later pass earns nothing more.
        award = XPAward(xp=terms["xp"], earning_id=UUID(operation))
        if (await apply_xp(operation, user_id, terms["subskillId"], award))["state"] == "recipient_erased":
            raise ErasedError
    if (await lock_user(user_id)).deleted:
        raise ErasedError
    state: EngineTaskState | None = await db.first(
        filter_by(EngineTaskState, user_id=user_id, unit_id=unit_id, goal_id=goal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await db.first(
        filter_by(EngineSubmission, id=submission_id, user_id=user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or state is None:
        raise ErasedError
    if row.state != "accepted":
        # Another evaluation booked it first, with the same verdict: evaluation is deterministic.
        return view(row, await room_completed(user_id, unit_id))

    now = utcnow()
    state.band, state.fails, state.passes = counted(document["bandRule"], state.band, state.fails, state.passes, passed)
    if state.start_id == row.start_id:
        # A verdict uses up its start; the next start is drawn in the band that holds then.
        state.start_id = state.start = state.start_band = None
    first = passed and not state.xp_applied
    if passed and state.passed_at is None:
        state.passed_at = now
    if first:
        state.xp_applied = True
        state.xp_operation_id, state.xp_amount, state.xp_skill_id = operation, terms["xp"], terms["subskillId"]
    state.updated_at = now
    row.state, row.open_goal_id, row.verdict = "booked", None, result["verdict"]
    row.feedback, row.counts, row.booked_at, row.evaluating_until = feedback(result, task), passed, now, None
    await db.session.flush()

    completed = await _complete_unit(user_id, unit_id, revision.tasks) if passed else False
    answer = view(row, completed)
    if first:
        answer["xp"] = {"awardId": operation, "xp": terms["xp"], "persisted": True}
    return answer
