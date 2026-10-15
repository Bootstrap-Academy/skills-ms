"""Rows of the lesson engine for tests: a revision of a unit, a learner's task state and their submissions."""

from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from api import models
from api.database import db
from api.utils.utc import utcnow

UNIT = "engine-unit"
GOAL = "reach"
HASH = "a" * 64
TASKS = [{"goalId": GOAL, "required": True, "xp": 1, "subskillId": "engine-subskill", "input": "program"}]
START = {
    "startId": "00000000-0000-4000-8000-000000000001",
    "goalId": GOAL,
    "caseId": "open-0",
    "config": {"width": 3},
    "initial": {"robot": {"x": 0}},
    "seed": "0" * 31 + "1",
}


def revision_id(unit_id: str = UNIT, number: int = 1) -> str:
    return str(uuid5(NAMESPACE_URL, f"engine-revision:{unit_id}:{number}"))


def module(unit_id: str = UNIT) -> models.LessonModule:
    return models.LessonModule(id=unit_id, api_version=1, entry_url=f"https://lessons.example/{unit_id}/entry.mjs")


def revision(unit_id: str = UNIT, number: int = 1, *, active: bool = True, **changes: Any) -> models.EngineRevision:
    values: dict[str, Any] = {
        "id": revision_id(unit_id, number),
        "unit_id": unit_id,
        "number": number,
        "active_for": unit_id if active else None,
        "module_id": unit_id,
        "package_hash": HASH,
        "manifest_hash": HASH,
        "entry_path": "index.html",
        "server_document": {"formatVersion": 1, "unitId": unit_id},
        "server_sha256": f"{number:064x}",
        "evidence_sha256": HASH,
        "tasks": TASKS,
        "created_at": utcnow(),
    }
    return models.EngineRevision(**{**values, **changes})


def task_state(user_id: str, unit_id: str = UNIT, goal_id: str = GOAL, **changes: Any) -> models.EngineTaskState:
    values: dict[str, Any] = {
        "user_id": user_id,
        "unit_id": unit_id,
        "goal_id": goal_id,
        "revision_id": revision_id(unit_id),
        "band": "plus",
        "fails": 0,
        "passes": 0,
        "start_id": START["startId"],
        "start": {**START, "revisionId": revision_id(unit_id)},
        "start_band": "plus",
        "start_request_id": None,
        "review_id": None,
        "passed_at": None,
        "xp_applied": False,
        "xp_operation_id": None,
        "xp_amount": None,
        "xp_skill_id": None,
        "updated_at": utcnow(),
    }
    return models.EngineTaskState(**{**values, **changes})


def submission(
    user_id: str,
    submission_id: str,
    unit_id: str = UNIT,
    goal_id: str = GOAL,
    *,
    booked: bool = True,
    input_hash: str = "b" * 64,
    **changes: Any,
) -> models.EngineSubmission:
    values: dict[str, Any] = {
        "id": submission_id,
        "user_id": user_id,
        "unit_id": unit_id,
        "goal_id": goal_id,
        "open_goal_id": None if booked else goal_id,
        "revision_id": revision_id(unit_id),
        "start_id": START["startId"],
        "input_hash": input_hash,
        "input": {"kind": "program", "source": "move();"},
        "job": {"revisionId": revision_id(unit_id), "publicStart": START, "band": "plus", "serverSha256": HASH},
        "state": "booked" if booked else "accepted",
        "verdict": "not_passed" if booked else None,
        "feedback": {"summary": "Prüf auch andere Weglängen.", "public_reasons": []} if booked else None,
        "counts": True if booked else None,
        "accepted_at": utcnow(),
        "booked_at": utcnow() if booked else None,
    }
    return models.EngineSubmission(**{**values, **changes})


async def add_engine_rows(user_id: str, unit_id: str = UNIT) -> None:
    """The unit's module and revision once, then a task state of this learner with one booked submission."""
    if await db.get(models.LessonModule, id=unit_id) is None:
        await db.add(module(unit_id))
    if await db.get(models.EngineRevision, id=revision_id(unit_id)) is None:
        await db.add(revision(unit_id))
    await db.add(task_state(user_id, unit_id))
    # Without a relationship the unit of work does not order these two inserts by their foreign key.
    await db.session.flush()
    await db.add(submission(user_id, str(uuid5(NAMESPACE_URL, f"engine-submission:{user_id}:{unit_id}")), unit_id))
    await db.session.flush()


# Rows the schema must refuse: every row but the last is valid, the last breaks one rule.
Violation = Callable[[], list[Any]]

VIOLATIONS: dict[str, Violation] = {
    "second active revision of a unit": lambda: [revision(), revision(number=2)],
    "same revision number twice": lambda: [
        revision(),
        revision(number=1, active=False, id="other-id", server_sha256=f"{9:064x}"),
    ],
    "same content twice": lambda: [revision(), revision(number=2, active=False, server_sha256=f"{1:064x}")],
    "active for another unit": lambda: [revision(active_for="other-unit")],
    "revision number zero": lambda: [revision(number=0)],
    "short package hash": lambda: [revision(package_hash="abc")],
    "negative fails": lambda: [revision(), task_state("learner", fails=-1)],
    "start without its band": lambda: [revision(), task_state("learner", start_band=None)],
    "xp applied without its operation": lambda: [revision(), task_state("learner", xp_applied=True)],
    "negative xp": lambda: [revision(), task_state("learner", xp_amount=-1)],
    "same plan for the same start twice": lambda: [
        revision(),
        task_state("learner"),
        submission("learner", "s-1"),
        submission("learner", "s-2"),
    ],
    "second open submission of a task": lambda: [
        revision(),
        task_state("learner"),
        submission("learner", "s-1", booked=False, input_hash="1" * 64),
        submission("learner", "s-2", booked=False, input_hash="2" * 64),
    ],
    "booked without verdict": lambda: [revision(), task_state("learner"), submission("learner", "s-1", verdict=None)],
    "booked but still open": lambda: [
        revision(),
        task_state("learner"),
        submission("learner", "s-1", open_goal_id="reach"),
    ],
    "accepted with a verdict": lambda: [
        revision(),
        task_state("learner"),
        submission("learner", "s-1", booked=False, verdict="passed"),
    ],
    "unknown state": lambda: [revision(), task_state("learner"), submission("learner", "s-1", state="running")],
    "unknown verdict": lambda: [revision(), task_state("learner"), submission("learner", "s-1", verdict="maybe")],
    "short input hash": lambda: [revision(), task_state("learner"), submission("learner", "s-1", input_hash="b")],
}
