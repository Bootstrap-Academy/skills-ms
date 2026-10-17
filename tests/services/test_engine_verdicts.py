"""Lesson engine: booking an evaluated submission (§9, §11, Ä28). Races run natively in `tests/engine_postgres.py`."""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid5

import pytest
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context, filter_by
from api.schemas.engine import ServerDocument
from api.services import engine_verdicts
from api.services.engine import task_terms
from api.services.engine_verdicts import ErasedError, book, counted, xp_operation_id
from api.services.sandbox import SandboxUnavailableError
from api.utils.utc import utcnow
from tests.engine_documents import SUBSKILL, UNIT, document
from tests.engine_rows import module, revision, start_id, submission, task_state

USER = "3f0c9a52-6a4e-4c1b-9d3e-5b8f2a7c1d01"
OPERATION = xp_operation_id(USER, UNIT, "reach")


def result(verdict: str = "passed", criteria: Any = None, codes: Any = None) -> dict[str, Any]:
    return {
        "formatVersion": 1,
        "verdict": verdict,
        "criteria": [{"id": "target", "passed": verdict == "passed"}] if criteria is None else criteria,
        "feedbackCodes": ([] if verdict == "passed" else ["boundary"]) if codes is None else codes,
        "runsHash": "f" * 64,
    }


def two_tasks() -> dict[str, Any]:
    """`reach` and `second` are required, `extra` is not."""
    value = document()
    value["tasks"].append({**deepcopy(value["tasks"][0]), "goalId": "second"})
    value["tasks"].append({**deepcopy(value["tasks"][0]), "goalId": "extra", "required": False})
    return value


async def seed(mocker: MockerFixture, server_document: dict[str, Any] | None = None, **state: Any) -> None:
    mocker.patch("api.services.benefits.get_user_status", AsyncMock(return_value=200))
    server_document = document() if server_document is None else server_document
    tasks = task_terms(ServerDocument.model_validate(server_document))
    async with db_context():
        await db.add(models.SubSkill(id=SUBSKILL, name="Engine"))
        await db.add(module())
        await db.add(revision(server_document=server_document, tasks=tasks))
        for task in tasks:
            await db.add(task_state(USER, goal_id=task["goalId"], **state))
        await db.add(
            models.RoomState(
                user_id=USER, unit_id=UNIT, revision=3, state={"plans": {}}, status="in_progress", updated_at=utcnow()
            )
        )


async def accepted(submission_id: str, goal_id: str = "reach", number: int = 1) -> None:
    """An accepted submission of the open start of the task (start `number`)."""
    async with db_context():
        await db.add(
            submission(
                USER,
                submission_id,
                goal_id=goal_id,
                booked=False,
                input_hash=f"{number:064x}",
                start_id=start_id(number),
            )
        )


async def next_start(number: int) -> None:
    """What the next task.start does after a verdict: a new public start of the task."""
    async with db_context():
        found = await db.get(models.EngineTaskState, user_id=USER, unit_id=UNIT, goal_id="reach")
        assert found is not None
        found.start_id, found.start, found.start_band = start_id(number), {"startId": start_id(number)}, "plus"


async def rows(goal_id: str = "reach") -> tuple[models.EngineTaskState, models.RoomState, int]:
    async with db_context():
        state = await db.get(models.EngineTaskState, user_id=USER, unit_id=UNIT, goal_id=goal_id)
        room = await db.get(models.RoomState, user_id=USER, unit_id=UNIT)
        xp = await models.XP.get_user_skill_xp(USER, SUBSKILL)
        assert state is not None and room is not None
        return state, room, xp


async def booked(submission_id: str, value: dict[str, Any]) -> dict[str, Any]:
    async with db_context():
        return await book(submission_id, USER, value)


def test_the_xp_operation_is_the_uuid5_of_the_canonical_name() -> None:
    namespace = UUID("4666e0ec-8a60-5ca8-8914-7d61fd6dc388")
    assert OPERATION == str(uuid5(namespace, f'["{USER}","{UNIT}","reach","xp"]'))
    assert xp_operation_id(USER, UNIT, "second") != OPERATION


@pytest.mark.parametrize(
    "band, fails, passes, passed, after",
    [
        ("plus", 0, 0, False, ("plus", 1, 0)),
        # The second fail in a row lowers the band one position; both counters start again.
        ("plus", 1, 0, False, ("base", 0, 0)),
        # At the lower edge the band stays, and the counters start again all the same.
        ("base", 1, 0, False, ("base", 0, 0)),
        # A pass ends a run of fails.
        ("plus", 1, 0, True, ("plus", 0, 1)),
        ("base", 0, 1, True, ("plus", 0, 0)),
        ("plus", 0, 1, True, ("plus", 0, 0)),
        ("base", 0, 1, False, ("base", 1, 0)),
    ],
)
def test_the_band_rule(band: str, fails: int, passes: int, passed: bool, after: tuple[str, int, int]) -> None:
    rule = {"bands": ["base", "plus"], "initial": "plus", "lowerAfter": 2, "raiseAfter": 2}
    assert counted(rule, band, fails, passes, passed) == after


async def test_a_pass_earns_the_xp_once_uses_up_the_start_and_completes_the_unit(mocker: MockerFixture) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    answer = await booked("s-1", result())
    assert answer == {
        "assessmentId": "s-1",
        "goalId": "reach",
        "status": "final",
        "outcome": "passed",
        "counts": True,
        # The frame says "Bestanden." itself; the summary adds nothing to that.
        "feedback": {"source": "server", "summary": "", "public_reasons": []},
        "completed": True,
        "xp": {"awardId": OPERATION, "xp": 1, "persisted": True},
    }
    state, room, xp = await rows()
    assert (state.band, state.fails, state.passes, state.start_id, state.start, state.start_band) == (
        "plus",
        0,
        1,
        None,
        None,
        None,
    )
    assert state.passed_at is not None
    assert (state.xp_applied, state.xp_operation_id, state.xp_amount, state.xp_skill_id) == (
        True,
        OPERATION,
        1,
        SUBSKILL,
    )
    # The room is completed without a new revision: only saves of the plan raise it (§9).
    assert (room.status, room.result, room.revision, xp) == ("completed", {"kind": "passed"}, 3, 1)
    # Booked once: the same result again only reads, without XP.
    again = await booked("s-1", result())
    assert again == {key: value for key, value in answer.items() if key != "xp"}
    assert (await rows())[2] == 1
    async with db_context():
        row = await db.get(models.EngineSubmission, id="s-1")
        assert row is not None
        assert (row.state, row.open_goal_id, row.verdict, row.counts, row.evaluating_until is None) == (
            "booked",
            None,
            "passed",
            True,
            True,
        )


async def test_a_later_pass_of_the_same_task_earns_nothing_more(mocker: MockerFixture) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    await booked("s-1", result())
    await next_start(2)
    await accepted("s-2", number=2)
    answer = await booked("s-2", result())
    # No XP in the answer, none on the account, and the unit stays completed.
    assert ("xp" in answer, answer["completed"]) == (False, True)
    state, room, xp = await rows()
    # The second pass in a row reaches the threshold; "plus" is the top band, so it stays.
    assert (state.band, state.fails, state.passes, xp, room.revision) == ("plus", 0, 0, 1, 3)


async def test_two_fails_in_a_row_lower_the_band_and_show_the_texts_of_the_package(mocker: MockerFixture) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    first = await booked("s-1", result("not_passed", codes=["boundary", "boundary"]))
    # A fail counts nothing, earns nothing and shows each general text once.
    assert first == {
        "assessmentId": "s-1",
        "goalId": "reach",
        "status": "final",
        "outcome": "not_passed",
        "counts": False,
        "feedback": {
            "source": "server",
            "summary": "Probier es noch einmal.",
            "public_reasons": ["Prüf auch andere Weglängen."],
        },
    }
    state, room, xp = await rows()
    assert (state.band, state.fails, state.passes, state.start, state.passed_at, xp, room.status) == (
        "plus",
        1,
        0,
        None,
        None,
        0,
        "in_progress",
    )
    await next_start(2)
    await accepted("s-2", number=2)
    await booked("s-2", result("not_passed"))
    state, room, xp = await rows()
    assert (state.band, state.fails, state.passes, state.xp_applied, room.status) == (
        "base",
        0,
        0,
        False,
        "in_progress",
    )


async def test_the_last_required_pass_completes_the_unit(mocker: MockerFixture) -> None:
    await seed(mocker, two_tasks(), start_id=start_id(1))
    await accepted("s-1")
    await accepted("s-2", goal_id="extra")
    first = await booked("s-1", result())
    optional = await booked("s-2", result())
    # The optional task earns its XP and does not complete the unit.
    assert ("completed" in first, "completed" in optional, optional["xp"]["awardId"]) == (
        False,
        False,
        xp_operation_id(USER, UNIT, "extra"),
    )
    assert (await rows())[1].status == "in_progress"
    await accepted("s-3", goal_id="second")
    last = await booked("s-3", result())
    assert last["completed"] is True
    _, room, xp = await rows()
    assert (room.status, room.result, room.revision, xp) == ("completed", {"kind": "passed"}, 3, 3)


@pytest.mark.parametrize(
    "value",
    [
        result(criteria=[{"id": "target", "passed": False}]),
        result(criteria=[]),
        result(criteria=[{"id": "target", "passed": True}, {"id": "target", "passed": True}]),
        result(criteria=[{"id": "target", "passed": True}, {"id": "other", "passed": True}]),
        result("not_passed", codes=["unknown"]),
    ],
)
async def test_a_result_that_does_not_fit_the_task_books_nothing(mocker: MockerFixture, value: dict[str, Any]) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    with pytest.raises(SandboxUnavailableError):
        await booked("s-1", value)
    async with db_context():
        row = await db.get(models.EngineSubmission, id="s-1")
        assert row is not None and row.state == "accepted"
        assert not await db.exists(filter_by(models.XPOperation, id=OPERATION))


async def test_an_erased_learner_gets_nothing_booked(mocker: MockerFixture) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    async with db_context():
        await db.add(models.PurchaseUser(user_id=USER, deleted=True))
    for value in (result(), result("not_passed")):
        with pytest.raises(ErasedError):
            async with db_context():
                # SQLite's legacy driver otherwise releases the first savepoint of apply_xp as a transaction.
                await db.exec("BEGIN")
                await book("s-1", USER, value)
    state, room, xp = await rows()
    assert (state.passed_at, state.fails, room.status, xp) == (None, 0, "in_progress", 0)
    async with db_context():
        assert not await db.exists(filter_by(models.XPOperation, id=OPERATION))
        assert (await db.get(models.EngineSubmission, id="s-1")).state == "accepted"  # type: ignore[union-attr]
    # Without the submission there is nothing to book either.
    with pytest.raises(ErasedError):
        await booked("gone", result())


async def test_a_failure_after_the_xp_rolls_the_whole_booking_back(mocker: MockerFixture) -> None:
    await seed(mocker, start_id=start_id(1))
    await accepted("s-1")
    mocker.patch.object(engine_verdicts, "_complete_unit", AsyncMock(side_effect=RuntimeError("synthetic")))
    with pytest.raises(RuntimeError):
        async with db_context():
            await db.exec("BEGIN")
            await book("s-1", USER, result())
    state, room, xp = await rows()
    assert (state.passed_at, state.xp_applied, state.start_id, room.status, xp) == (
        None,
        False,
        start_id(1),
        "in_progress",
        0,
    )
    async with db_context():
        assert not await db.exists(filter_by(models.XPOperation, id=OPERATION))
        assert (await db.get(models.EngineSubmission, id="s-1")).state == "accepted"  # type: ignore[union-attr]
