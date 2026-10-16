"""Native races of account erasure and XP delivery receipts, against an already-started disposable PostgreSQL.

DATABASE_URL must point to an empty local database created only for this run.
Run: DAILY_TEST_POSTGRES_BIN=/path/to/postgresql/bin nix develop --command \
    python tests/run_daily_limit_postgres.py tests.xp_receipts_postgres
"""

import asyncio
import os
from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from alembic import command
from alembic.config import Config

from sqlalchemy import text

from api import models, sweep
from api.database import db, db_context, delete, filter_by, select
from api.services import benefits
from api.services.benefits import XPAward, apply_xp
from api.services.purchases import lock_user
from api.services.user_deletion import delete_user_data
from api.settings import settings

SKILL = "receipt-skill"
AWARD = XPAward(xp=17, earning_id=uuid4())


async def deliver(operation: str, user_id: str) -> dict[str, Any]:
    async with db_context():
        return await apply_xp(operation, user_id, SKILL, AWARD)


async def leftovers(user_id: str) -> tuple[int, int]:
    """Receipts and XP rows that remain of an account."""
    async with db_context():
        return (
            await db.count(filter_by(models.XPOperation, user_id=user_id)),
            await db.count(filter_by(models.XP, user_id=user_id)),
        )


async def holding_erasure(user_id: str, holding: asyncio.Event, release: asyncio.Event) -> None:
    """Erases the account and keeps the transaction (and the user guard) open until `release`."""

    async def wait(prefix: str) -> None:
        holding.set()
        await release.wait()

    with patch("api.services.user_deletion.clear_cache", wait):
        async with db_context():
            await delete_user_data(user_id)


async def erase(user_id: str) -> None:
    async with db_context():
        await delete_user_data(user_id)


async def refused_later(operation: str, user_id: str, result: dict[str, Any]) -> None:
    """A late repetition of the original delivery is refused, grants nothing and records nothing."""
    refusal = {**result, "state": "recipient_erased", "applied": False}
    assert await deliver(operation, user_id) == refusal
    assert await leftovers(user_id) == (0, 0)


async def erasure_while_a_delivery_holds_its_receipt() -> None:
    """A repeated delivery holds its receipt and then takes the user guard, as a booking of the lesson engine does.
    An erasure that starts meanwhile waits for it instead of deadlocking, then removes the receipt."""
    user_id, operation = str(uuid4()), str(uuid4())
    result = await deliver(operation, user_id)
    held, go = asyncio.Event(), asyncio.Event()

    async def repeat_then_guard() -> None:
        async with db_context():
            assert await apply_xp(operation, user_id, SKILL, AWARD) == result
            held.set()
            await go.wait()
            assert not (await lock_user(user_id)).deleted

    holder = asyncio.create_task(repeat_then_guard())
    await held.wait()
    erasure = asyncio.create_task(erase(user_id))
    await asyncio.sleep(0.3)
    assert not erasure.done(), "the erasure did not wait for the delivery holding the receipt"
    go.set()
    await asyncio.gather(holder, erasure)
    assert await leftovers(user_id) == (0, 0)
    await refused_later(operation, user_id, result)


async def erasure_during_a_new_delivery() -> None:
    """A new delivery claims its receipt and waits for the guard of a running erasure. It is refused and its
    claim goes again; the receipt of an earlier delivery went with the erasure."""
    user_id, earlier, operation = str(uuid4()), str(uuid4()), str(uuid4())
    earlier_result = await deliver(earlier, user_id)
    holding, release = asyncio.Event(), asyncio.Event()
    erasure = asyncio.create_task(holding_erasure(user_id, holding, release))
    await holding.wait()
    waiting = asyncio.create_task(deliver(operation, user_id))
    await asyncio.sleep(0.3)
    assert not waiting.done(), "the delivery did not wait for the erasure"
    release.set()
    await erasure
    result = await waiting
    assert (result["state"], result["applied"]) == ("recipient_erased", False), result
    assert await leftovers(user_id) == (0, 0)
    await refused_later(earlier, user_id, earlier_result)


async def erasure_after_a_claim() -> None:
    """A delivery has claimed its receipt but not yet reached the guard when the erasure starts. The erasure does
    not wait for the claim; the delivery then finds the tombstone, and nothing remains."""
    user_id, operation = str(uuid4()), str(uuid4())
    claimed, go = asyncio.Event(), asyncio.Event()

    async def late_guard(subject: str) -> models.PurchaseUser:
        claimed.set()
        await go.wait()
        return await lock_user(subject)

    with patch.object(benefits, "lock_user", late_guard):
        waiting = asyncio.create_task(deliver(operation, user_id))
        await claimed.wait()
        holding, release = asyncio.Event(), asyncio.Event()
        erasure = asyncio.create_task(holding_erasure(user_id, holding, release))
        await asyncio.wait_for(holding.wait(), 5)
        go.set()
        await asyncio.sleep(0.3)
        assert not waiting.done(), "the delivery did not wait for the erasure"
        release.set()
        await erasure
        result = await waiting
    assert (result["state"], result["applied"]) == ("recipient_erased", False), result
    assert await leftovers(user_id) == (0, 0)


async def delivery_ahead_of_the_erasure() -> None:
    """A delivery holds the user guard when the erasure starts. It is applied and committed first; the erasure
    then removes its receipt together with the XP."""
    user_id, operation = str(uuid4()), str(uuid4())
    inside, go = asyncio.Event(), asyncio.Event()

    async def status(subject: str) -> int:
        inside.set()
        await go.wait()
        return 200

    with patch.object(benefits, "get_user_status", status):
        waiting = asyncio.create_task(deliver(operation, user_id))
        await inside.wait()
        erasure = asyncio.create_task(erase(user_id))
        await asyncio.sleep(0.3)
        assert not erasure.done(), "the erasure did not wait for the delivery holding the guard"
        go.set()
        result = await waiting
        await erasure
    assert result["state"] == "applied", result
    assert await leftovers(user_id) == (0, 0)
    await refused_later(operation, user_id, result)


async def sweep_of_an_earlier_erasure() -> None:
    """Receipts an earlier erasure kept (tombstone and receipts, nothing else) go with the nightly sweep; the
    receipts of a live account stay."""
    erased, alive = str(uuid4()), str(uuid4())
    for user_id in (erased, erased, alive):
        await deliver(str(uuid4()), user_id)
    async with db_context():
        guard = await lock_user(erased)
        guard.deleted = True
        await db.exec(delete(models.XP).where(models.XP.user_id == erased))

    async def status(user_id: str) -> int:
        return 404 if user_id == erased else 200

    with patch.object(sweep, "get_user_status", status), patch.object(settings, "deleted_user_sweep_rate_limit", 0):
        await sweep.sweep_deleted_users()
    assert await leftovers(erased) == (0, 0)
    assert await leftovers(alive) == (1, 1)


async def main() -> None:
    url = os.environ["DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://")
    async with db_context():
        version = (await db.exec(text("SHOW server_version"))).scalar_one()
        await db.add(models.SubSkill(id=SKILL, name="Receipts"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(benefits, "get_user_status", AsyncMock(return_value=200)))
        stack.enter_context(patch("api.services.user_deletion.clear_cache", AsyncMock()))
        await erasure_while_a_delivery_holds_its_receipt()
        await erasure_during_a_new_delivery()
        await erasure_after_a_claim()
        await delivery_ahead_of_the_erasure()
        await sweep_of_an_earlier_erasure()
    async with db_context():
        # Only the live account of the sweep scenario keeps a receipt.
        assert await db.count(select(models.XPOperation)) == 1
    print(
        f"XP receipts on PostgreSQL {version}: erasure while a delivery holds its receipt, during a new delivery, "
        "after a claim, after a delivery holding the guard, late repetitions refused, sweep of an earlier erasure"
    )


if __name__ == "__main__":
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(main())
