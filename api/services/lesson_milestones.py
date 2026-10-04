"""Lesson milestones (XP-02): a unit's milestone books XP in challenges-ms once per learner, eventually.

Only two completions count: an exact server-side answer check (`deterministic`, first round only) and a passing
verdict that llm-ms signed and this service verified (`llm_verdict`, also in a repeat round). Skips and every
completion without such a check (such as the fallback without the model, which completes an LLM-graded unit
without a verdict) never book anything themselves.

The outbox row is the record that a learner's milestone was queued: it is written in the completing
transaction and kept until the account is erased, so a unit books at most once. A learner who finished an
LLM-graded unit through the fallback or a skip therefore earns its milestone with the first verified pass in a
later repeat round, and a second verified pass finds the row and books nothing (PO 24.09.).

The completion commits first with an outbox row; delivery runs afterwards and is retried with backoff until
challenges-ms answers for good. challenges-ms books once per learner and unit, so resending is harmless.
"""

import asyncio
from contextvars import ContextVar
from datetime import timedelta
from typing import Any, Literal

import httpx

from api.database import db, db_context, db_wrapper, filter_by
from api.logger import get_logger
from api.models import LessonMilestoneDelivery
from api.schemas.rooms import CatalogueUnit, Complete, IntroductionCompletion, LlmVerdictCompletion
from api.services import llm
from api.settings import settings
from api.utils.jwt import encode_jwt
from api.utils.utc import utcnow

logger = get_logger(__name__)

MilestoneCompletion = Literal["deterministic", "llm_verdict"]
Outcome = Literal["delivered", "erased", "rejected", "retry"]

FIRST_RETRY = timedelta(seconds=30)
LAST_RETRY = timedelta(hours=1)

# Deliveries started right after a completion; kept so they are not garbage-collected mid-flight.
_running: set["asyncio.Task[None]"] = set()
# The milestone queued by the current request, so the endpoint can send it once the completion committed.
_queued: ContextVar[tuple[str, str] | None] = ContextVar("lesson_milestone_queued", default=None)


def checked_completion(
    unit: CatalogueUnit, data: Complete, verdict: llm.VerdictClaims | None, *, repeat: bool
) -> MilestoneCompletion | None:
    """How a completion of this unit was checked, if it may earn the unit's milestone at all.

    `enqueue` then books it only if this learner has no outbox row for the unit yet.
    """
    if unit.milestone is None or data.action != "complete" or unit.exercise is not None:
        return None
    if isinstance(unit.completion, LlmVerdictCompletion):
        # Only the verified, passing verdict counts; the fallback without the model (no verdict) never does.
        # It counts in a repeat round too: an outage or a skip in the first round must not cost the milestone
        # for good (PO 24.09.).
        return "llm_verdict" if verdict is not None and verdict.passed else None
    if isinstance(unit.completion, IntroductionCompletion) and not repeat:
        return "deterministic"
    return None


async def enqueue(user_id: str, unit: CatalogueUnit, completion: MilestoneCompletion) -> None:
    """Add the outbox row in the caller's transaction, next to the completed room state; once per learner and unit.

    An existing row in any state (also `rejected`) means the milestone was queued before, so nothing is added.
    """
    if unit.milestone is None or await db.get(LessonMilestoneDelivery, user_id=user_id, unit_id=unit.id) is not None:
        return
    now = utcnow()
    await db.add(
        LessonMilestoneDelivery(
            user_id=user_id,
            unit_id=unit.id,
            skill_id=unit.milestone.skill_id,
            xp=unit.milestone.xp,
            completion=completion,
            state="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )
    _queued.set((user_id, unit.id))


def take_queued() -> tuple[str, str] | None:
    """The (user, unit) milestone queued by this request, if any; clears the mark."""
    queued = _queued.get()
    _queued.set(None)
    return queued


def backoff(attempts: int) -> timedelta:
    # 30 s, 1 min, 2 min, ... up to one hour; the exponent is capped so the product never overflows.
    return min(FIRST_RETRY * (1 << min(max(attempts - 1, 0), 7)), LAST_RETRY)


def request_body(row: LessonMilestoneDelivery) -> dict[str, Any]:
    return {"skill_id": row.skill_id, "xp": row.xp, "completion": row.completion}


async def send(user_id: str, unit_id: str, body: dict[str, Any]) -> tuple[Outcome, int | None]:
    """One call to challenges-ms; the outcome says whether to stop or to try again later."""
    token = encode_jwt(
        {"aud": "challenges"},
        timedelta(seconds=settings.internal_jwt_ttl),
        secret=settings.internal_jwt_secret("challenges"),
    )
    url = settings.challenges_url.rstrip("/") + f"/_internal/lesson-milestones/{user_id}/{unit_id}"
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False) as client:
            response = await client.put(url, json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError:
        return "retry", None
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    if status == 200:
        # `created: false` is a success too: the milestone was already booked (first booking wins).
        milestone = data.get("milestone") if isinstance(data, dict) else None
        if (
            isinstance(milestone, dict)
            and isinstance(data.get("created"), bool)
            and milestone.get("unit_id") == unit_id
        ):
            return "delivered", status
        return "retry", status
    if status == 410:
        return "erased", status
    # A missing route (challenges-ms not deployed yet) is also a 404; only the named content error is final.
    if (status == 404 and isinstance(data, dict) and data.get("error") == "skill_not_found") or status == 422:
        return "rejected", status
    return "retry", status


async def deliver(user_id: str, unit_id: str) -> None:
    """Send one due milestone. Runs in its own database context; never inside the completing request."""
    query = (
        filter_by(LessonMilestoneDelivery, user_id=user_id, unit_id=unit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = await db.first(query)
    if row is None or row.state != "pending" or row.next_attempt_at > utcnow():
        return
    # Claim before calling: a concurrent sender sees the next attempt in the future and skips.
    row.attempts += 1
    row.next_attempt_at = utcnow() + backoff(row.attempts)
    body = request_body(row)
    await db.commit()

    outcome, status = await send(user_id, unit_id, body)

    row = await db.first(query)
    if row is None or row.state != "pending":
        # Erased with the account meanwhile, or finished by another sender.
        await db.commit()
        return
    row.last_status = status
    if outcome != "retry":
        row.state = outcome
        row.finished_at = utcnow()
    if outcome == "rejected":
        logger.error("Lesson milestone rejected by challenges-ms (%s): unit %s", status, unit_id)
    elif outcome == "retry":
        logger.warning("Lesson milestone retained for a later attempt (%s): unit %s", status, unit_id)
    await db.commit()


@db_wrapper
async def recover() -> None:
    due = await db.all(
        filter_by(LessonMilestoneDelivery, state="pending")
        .where(LessonMilestoneDelivery.next_attempt_at <= utcnow())
        .order_by(LessonMilestoneDelivery.next_attempt_at)
    )
    for user_id, unit_id in [(row.user_id, row.unit_id) for row in due]:
        try:
            await deliver(user_id, unit_id)
        except Exception:
            await db.session.rollback()
            logger.exception("Lesson milestone delivery retained: unit %s", unit_id)


async def _deliver_now(user_id: str, unit_id: str) -> None:
    try:
        async with db_context():
            await deliver(user_id, unit_id)
    except Exception:
        # The recovery loop tries again; the completion itself is already committed.
        logger.exception("Lesson milestone delivery retained: unit %s", unit_id)


def deliver_soon(user_id: str, unit_id: str) -> None:
    """Start delivery after the completion committed, without holding up the response."""
    task = asyncio.create_task(_deliver_now(user_id, unit_id))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def settle() -> None:
    """Wait for deliveries started by `deliver_soon` (tests and shutdown)."""
    while _running:
        await asyncio.gather(*list(_running), return_exceptions=True)
