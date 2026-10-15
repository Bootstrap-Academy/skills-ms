"""Native races of evaluation and booking in the lesson engine (PLAN.md §8, BERICHT §10.5 point 5), against an
already-started disposable PostgreSQL.

DATABASE_URL must point to an empty local database created only for this run.
Run: DAILY_TEST_POSTGRES_BIN=/path/to/postgresql/bin nix develop --command \
    python tests/run_daily_limit_postgres.py tests.engine_verdicts_postgres
"""

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

from alembic import command
from alembic.config import Config

from fastapi import HTTPException
from sqlalchemy import text

from api import models
from api.database import db, db_context, filter_by, select
from api.schemas.engine import ServerDocument, SubmitRequest
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import engine as engine_service
from api.services import engine_tasks, engine_verdicts, rooms, sandbox
from api.services.engine_verdicts import ErasedError, book, xp_operation_id
from api.services.user_deletion import delete_user_data
from api.settings import settings
from api.utils.utc import utcnow
from tests.endpoints.engine.support import ENGINE_UNIT, ORIGIN, unit
from tests.engine_documents import SUBSKILL, UNIT, document
from tests.engine_rows import module, revision, start_id, submission, task_state

PASS = {
    "formatVersion": 1,
    "verdict": "passed",
    "criteria": [{"id": "target", "passed": True}],
    "feedbackCodes": [],
    "runsHash": "f" * 64,
}
FAIL = {**PASS, "verdict": "not_passed", "criteria": [{"id": "target", "passed": False}], "feedbackCodes": ["boundary"]}
PLAN = {"kind": "program", "source": "while (frontIsClear()) move();"}
CATALOGUE = Catalogue.model_validate(
    {
        "paths": [{"id": "engine-path", "title": {"de": "Pfad", "en": "Path"}, "units": [UNIT, "plain"]}],
        "units": [ENGINE_UNIT, unit("plain")],
    }
)


async def seed_unit() -> None:
    server_document = document()
    async with db_context():
        await db.add(models.SubSkill(id=SUBSKILL, name="Engine"))
        await db.add(module())
        # Without a relationship the unit of work does not order the revision after its module.
        await db.session.flush()
        await db.add(
            revision(
                server_document=server_document,
                tasks=engine_service.task_terms(ServerDocument.model_validate(server_document)),
            )
        )


async def seed_learner(user_id: str, *, band: str = "base", passes: int = 1, accepted: bool = True) -> None:
    """A learner on the open start of `reach`, one pass short of the higher band, the plan saved, and (unless
    `accepted` is false) its submission accepted. The user guard exists, as after any start."""
    async with db_context():
        await db.add(models.PurchaseUser(user_id=user_id, deleted=False))
        await db.add(task_state(user_id, band=band, passes=passes))
        await db.add(
            models.RoomState(
                user_id=user_id,
                unit_id=UNIT,
                revision=1,
                state={"plans": {"reach": PLAN}},
                status="in_progress",
                updated_at=utcnow(),
            )
        )
        await db.session.flush()
        if accepted:
            await db.add(submission(user_id, f"{user_id}-s", booked=False, input=PLAN))


async def leftovers(user_id: str) -> dict[str, int]:
    """What remains of a learner: engine rows, room, XP and the receipt of the XP operation."""
    async with db_context():
        counts = {
            model.__name__: await db.count(filter_by(model, user_id=user_id))
            for model in (models.EngineSubmission, models.EngineTaskState, models.RoomState, models.XP)
        }
        counts["XPOperation"] = await db.count(
            filter_by(models.XPOperation, id=xp_operation_id(user_id, UNIT, "reach"))
        )
        return counts


async def booked_once(user_id: str, answers: list[dict[str, Any]]) -> None:
    """One booking: one answer carries the XP, one XP effect on the account, and the band moved exactly once."""
    assert sum("xp" in answer for answer in answers) == 1, answers
    async with db_context():
        assert await models.XP.get_user_skill_xp(user_id, SUBSKILL) == 1
        receipts = await db.count(filter_by(models.XPOperation, user_id=user_id))
        state = await db.get(models.EngineTaskState, user_id=user_id, unit_id=UNIT, goal_id="reach")
        row = await db.get(models.EngineSubmission, id=f"{user_id}-s")
        room = await db.get(models.RoomState, user_id=user_id, unit_id=UNIT)
        assert receipts == 1
        assert state is not None
        counters = (state.band, state.fails, state.passes, state.xp_applied)
        assert counters == ("plus", 0, 0, True), f"band and counters after the booking: {counters}"
        assert row is not None and (row.state, row.verdict) == ("booked", "passed")
        assert room is not None and (room.status, room.result, room.revision) == ("completed", {"kind": "passed"}, 1)


def slow_sandbox(answer: dict[str, Any], calls: list[int], seconds: float = 0.3) -> Callable[..., Awaitable[Any]]:
    async def evaluate(server_document: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        await asyncio.sleep(seconds)
        return dict(answer)

    return evaluate


async def ten_status_reads() -> None:
    """Ten reads of a pending submission at once: one evaluation under the lease, one booking."""
    user = User(id="reads", email_verified=True, admin=False)
    await seed_learner(user.id)
    calls: list[int] = []

    async def read() -> dict[str, Any]:
        async with db_context():
            return await engine_tasks.status(f"{user.id}-s", user)

    with patch.object(sandbox, "evaluate", slow_sandbox(PASS, calls)):
        answers = await asyncio.gather(*(read() for _ in range(10)))
    assert len(calls) == 1, calls
    await booked_once(user.id, answers)


async def lost_leases() -> None:
    """Ten evaluations that each outlived their lease book the same result at once: one booking."""
    user_id = "leases"
    await seed_learner(user_id)

    answers = await asyncio.gather(*(booking(user_id, PASS) for _ in range(10)))
    assert all(answer["status"] == "final" and answer["outcome"] == "passed" for answer in answers), answers
    await booked_once(user_id, answers)


async def failure_after_xp() -> None:
    """A failure after apply_xp rolls the whole booking back, its receipt included."""
    user_id = "rollback"
    await seed_learner(user_id)
    with patch.object(engine_verdicts, "_complete_unit", AsyncMock(side_effect=RuntimeError("synthetic"))):
        try:
            async with db_context():
                await book(f"{user_id}-s", user_id, dict(PASS))
        except RuntimeError:
            pass
        else:
            raise AssertionError("the booking did not fail")
    assert await leftovers(user_id) == {
        "EngineSubmission": 1,
        "EngineTaskState": 1,
        "RoomState": 1,
        "XP": 0,
        "XPOperation": 0,
    }
    async with db_context():
        row = await db.get(models.EngineSubmission, id=f"{user_id}-s")
        state = await db.get(models.EngineTaskState, user_id=user_id, unit_id=UNIT, goal_id="reach")
        assert row is not None and row.state == "accepted"
        assert state is not None and (state.band, state.passes, state.passed_at, state.start_id) == (
            "base",
            1,
            None,
            start_id(1),
        )


ERASED = {"EngineSubmission": 0, "EngineTaskState": 0, "RoomState": 0, "XP": 0, "XPOperation": 0}


async def erasure_during_evaluation() -> None:
    """The account is erased while the sandbox runs: the late result finds nothing and writes nothing."""
    user = User(id="erased-evaluating", email_verified=True, admin=False)
    await seed_learner(user.id)
    running, erased = asyncio.Event(), asyncio.Event()

    async def evaluate(server_document: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        running.set()
        await erased.wait()
        return dict(PASS)

    async def read() -> dict[str, Any]:
        async with db_context():
            return await engine_tasks.status(f"{user.id}-s", user)

    with patch.object(sandbox, "evaluate", evaluate):
        reading = asyncio.create_task(read())
        await running.wait()
        async with db_context():
            await delete_user_data(user.id)
        erased.set()
        try:
            await reading
        except HTTPException as error:
            assert error.status_code == 401, error
        else:
            raise AssertionError("a result was booked after the erasure")
    assert await leftovers(user.id) == ERASED


async def holding_erasure(user_id: str, holding: asyncio.Event, release: asyncio.Event) -> None:
    """Erases the account and keeps the transaction (and the user guard) open until `release`."""

    async def wait(prefix: str) -> None:
        holding.set()
        await release.wait()

    with patch("api.services.user_deletion.clear_cache", wait):
        async with db_context():
            await delete_user_data(user_id)


async def booking(user_id: str, verdict: dict[str, Any]) -> dict[str, Any]:
    async with db_context():
        return await book(f"{user_id}-s", user_id, dict(verdict))


async def erasure_during_booking() -> None:
    """A booking waits for the user guard of a running erasure, then books nothing, its XP receipt neither."""
    for verdict in (PASS, FAIL):
        user_id = f"erased-booking-{verdict['verdict']}"
        await seed_learner(user_id)
        holding, release = asyncio.Event(), asyncio.Event()
        erasure = asyncio.create_task(holding_erasure(user_id, holding, release))
        await holding.wait()

        waiting = asyncio.create_task(booking(user_id, verdict))
        await asyncio.sleep(0.3)
        assert not waiting.done(), "the booking did not wait for the erasure"
        release.set()
        await erasure
        try:
            await waiting
        except ErasedError:
            pass
        else:
            raise AssertionError("a booking went through after the erasure")
        assert await leftovers(user_id) == ERASED, verdict["verdict"]


def submit_request(plan: dict[str, Any], request_id: str) -> SubmitRequest:
    return SubmitRequest.model_validate(
        {"requestId": request_id, "stateRevision": 1, "answer": {"startId": start_id(1), "input": plan}}
    )


async def submitting(user: User, plan: dict[str, Any], request_id: str) -> int:
    """A submission as the route makes it; its HTTP status."""
    try:
        async with db_context():
            await engine_service.load_active_units()
            _, status = await engine_tasks.submit(UNIT, "reach", submit_request(plan, request_id), user)
            return status
    except HTTPException as error:
        return error.status_code


async def erasure_during_submission() -> None:
    """A submission waits for the user guard of a running erasure and is then refused; nothing stays."""
    user = User(id="erased-submitting", email_verified=True, admin=False)
    await seed_learner(user.id, accepted=False)
    holding, release = asyncio.Event(), asyncio.Event()
    erasure = asyncio.create_task(holding_erasure(user.id, holding, release))
    await holding.wait()
    waiting = asyncio.create_task(submitting(user, PLAN, "submit-1"))
    await asyncio.sleep(0.3)
    assert not waiting.done(), "the submission did not wait for the erasure"
    release.set()
    await erasure
    assert await waiting == 401
    assert await leftovers(user.id) == ERASED


async def two_submissions() -> None:
    """Two different plans for the same start at once: one is accepted, the other refused (§9, Ä37)."""
    user = User(id="two-plans", email_verified=True, admin=False)
    await seed_learner(user.id, accepted=False)
    other = {"kind": "program", "source": "move();"}
    statuses = await asyncio.gather(submitting(user, PLAN, "submit-1"), submitting(user, other, "submit-2"))
    assert sorted(statuses) == [202, 409], statuses
    async with db_context():
        rows = await db.all(filter_by(models.EngineSubmission, user_id=user.id))
        assert [row.input for row in rows] == [PLAN]


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://")
    async with db_context():
        version = (await db.exec(text("SHOW server_version"))).scalar_one()
    await seed_unit()
    with ExitStack() as stack:
        stack.enter_context(patch("api.services.benefits.get_user_status", AsyncMock(return_value=200)))
        stack.enter_context(patch("api.services.user_deletion.clear_cache", AsyncMock()))
        stack.enter_context(patch.object(rooms, "load_catalogue", lambda: CATALOGUE))
        for name, value in {
            "sandkasten_url": "http://sandbox.test",
            "rooms_enabled": True,
            "engine_enabled": True,
            "engine_content_origin": ORIGIN,
            "learning_rooms_exercise_refs": {},
        }.items():
            stack.enter_context(patch.object(settings, name, value))
        await ten_status_reads()
        await lost_leases()
        await failure_after_xp()
        await erasure_during_evaluation()
        await erasure_during_booking()
        # Without a sandbox a submission only waits; these two are about the guard and the start.
        stack.enter_context(patch.object(settings, "sandkasten_url", None))
        await erasure_during_submission()
        await two_submissions()
    async with db_context():
        # One receipt each for the reads and the leases; every other scenario left none.
        assert await db.count(select(models.XPOperation)) == 2
    print(
        f"engine verdicts on PostgreSQL {version}: ten status reads (one evaluation, one booking), ten lost leases "
        "(one booking), rollback after apply_xp, erasure during evaluation, booking (pass and fail) and submission, "
        "two plans for one start"
    )


if __name__ == "__main__":
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(main())
