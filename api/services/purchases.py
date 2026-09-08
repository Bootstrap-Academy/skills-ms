"""Exact course offers and durable keyed purchase recovery.

The backend acceptance/financial commit is authoritative. Local 'prepared' means
only that the command can be replayed, never that money or acceptance is proved.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, StrictBool
from sqlalchemy.exc import IntegrityError

from api.database import db, db_wrapper, filter_by
from api.logger import get_logger
from api.models.course_access import CourseAccess
from api.models.purchase import CoursePurchase, PurchaseUser
from api.schemas.course import Course
from api.services.auth import get_user_status
from api.services.internal import InternalService
from api.utils.cache import clear_cache


logger = get_logger(__name__)


class Acceptance(BaseModel):
    order_id: UUID
    offer_hash: str
    accepted: StrictBool
    early_performance_requested: StrictBool

    def payload(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self.json()))


async def lock_user(user_id: str) -> PurchaseUser:
    # The same durable row serializes every course order and T10 deletion.
    if await db.get(PurchaseUser, user_id=user_id) is None:
        try:
            async with db.session.begin_nested():
                await db.add(PurchaseUser(user_id=user_id, deleted=False))
                await db.session.flush()
        except IntegrityError:
            pass
    row = await db.first(
        filter_by(PurchaseUser, user_id=user_id).with_for_update().execution_options(populate_existing=True)
    )
    assert row is not None
    return cast(PurchaseUser, row)


def product(course: Course) -> dict[str, Any]:
    facts = {
        "course_id": course.id,
        "course": json.loads(course.summary(None).json()),
        "access": "Einzelzugang zu diesem Kurs gemäß dem hier wiedergegebenen Angebot und den beigefügten AGB; keine automatische Verlängerung.",
    }
    description = "\n".join(
        [
            course.description or "",
            facts["access"],
            "Sprache: " + (course.language or "wie im Kurs angegeben"),
            "Lernziele: " + "; ".join(course.learning_goals),
            "Voraussetzungen: " + "; ".join(course.requirements),
            "Inhalte: "
            + "; ".join(
                section.title + ": " + ", ".join(lecture.title for lecture in section.lectures)
                for section in course.sections
            ),
            "Gesamte Videodauer: "
            + str(sum(lecture.duration for section in course.sections for lecture in section.lectures))
            + " Sekunden.",
            "Bereitstellung des Zugangs nach Vertragsbestätigung; die Bestellung bleibt bei ausstehender Bereitstellung zur Klärung erhalten.",
        ]
    )
    revision = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {
        "kind": "course",
        "reference": course.id,
        "title": course.title,
        "description": description,
        "coins": course.price,
        "facts": facts,
        "revision": revision,
        "service_starts_at": None,
    }


async def offer(user_id: str, course: Course) -> dict[str, Any]:
    guard = await lock_user(user_id)
    if guard.deleted or course.free:
        raise HTTPException(412, "Purchase unavailable")
    if await db.exists(filter_by(CourseAccess, user_id=user_id, course_id=course.id)):
        raise HTTPException(409, "Course already owned")
    pending = await db.first(
        filter_by(CoursePurchase, user_id=user_id, course_id=course.id).where(
            CoursePurchase.state.in_(["prepared", "paid", "review"])
        )
    )
    if pending is not None:
        return {"offer": pending.offer, "state": pending.state, "fulfillment": pending.result}
    description = product(course)
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/purchase-offers/skills/{user_id}", json=description)
        if response.status_code != 200:
            raise HTTPException(
                response.status_code if response.status_code in (409, 412) else 503, "Offer unavailable"
            )
        result = response.json()
    await db.add(
        CoursePurchase(
            id=result["offer"]["id"],
            user_id=user_id,
            course_id=course.id,
            state="offered",
            offer=result["offer"],
            created_at=datetime.now(timezone.utc),
        )
    )
    return cast(dict[str, Any], result)


async def buy(user_id: str, course: Course, acceptance: Acceptance) -> dict[str, Any]:
    guard = await lock_user(user_id)
    row = await db.get(CoursePurchase, id=str(acceptance.order_id), user_id=user_id, course_id=course.id)
    if guard.deleted or row is None:
        raise HTTPException(404, "Purchase not found")
    payload = acceptance.payload()
    if row.acceptance is not None:
        if row.acceptance != payload:
            raise HTTPException(409, "Conflicting acceptance")
    else:
        if row.state != "offered" or row.offer["product"] != product(course):
            raise HTTPException(409, "Offer changed; request a new offer")
        if (
            not acceptance.accepted
            or not acceptance.early_performance_requested
            or acceptance.offer_hash != row.offer["hash"]
        ):
            raise HTTPException(409, "Exact offer acceptance required")
        if await db.exists(filter_by(CourseAccess, user_id=user_id, course_id=course.id)):
            raise HTTPException(409, "Course already owned")
        if await db.exists(
            filter_by(CoursePurchase, user_id=user_id, course_id=course.id).where(
                CoursePurchase.state.in_(["prepared", "paid", "review", "fulfilled"]), CoursePurchase.id != row.id
            )
        ):
            raise HTTPException(409, "Another course purchase already exists; retain its order identity")
        row.acceptance = payload
        row.state = "prepared"
        row.active_key = user_id + ":" + course.id
    order_id = row.id
    try:
        await db.commit()
    except IntegrityError:
        # Unique active ownership is the final authority even on repeatable-read
        # databases whose earlier ordinary reads predate a waiting lock.
        await db.session.rollback()
        existing = await db.first(
            filter_by(CoursePurchase, active_key=user_id + ":" + course.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is None:
            raise
        return {"offer": existing.offer, "state": existing.state, "fulfillment": existing.result}

    await deliver(order_id)
    row = await db.get(CoursePurchase, id=order_id)
    assert row is not None
    return {"offer": row.offer, "state": row.state, "fulfillment": row.result}


async def deliver(order_id: str) -> None:
    row = await db.get(CoursePurchase, id=order_id)
    if row is None:
        return
    guard = await lock_user(row.user_id)
    row = await db.first(
        filter_by(CoursePurchase, id=order_id).with_for_update().execution_options(populate_existing=True)
    )
    assert row is not None
    if row.state == "fulfilled":
        await report(row)
        return
    if row.state not in ("prepared", "paid"):
        return
    if guard.deleted:
        row.state = "review"
        await db.commit()
        return
    # Query the uncached authority: a historical successful debit cannot recreate
    # access after the live account vanished, even before erasure fanout arrives.
    status = await get_user_status(row.user_id)
    if status == 404:
        guard.deleted = True
        row.state = "review"
        await db.commit()
        return
    if status != 200:
        return
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/purchases/skills/{row.user_id}", json=row.acceptance)
    if response.status_code in (409, 412):
        # Backend rejection before acceptance; it has made no financial effect.
        row.state = "failed"
        row.active_key = None
    elif response.status_code == 200:
        outcome = response.json()
        row.result = outcome
        row.state = outcome["state"]
        if row.state == "failed":
            row.active_key = None
        if row.state == "paid" and outcome.get("confirmation_smtp_accepted_at"):
            if not await db.exists(filter_by(CourseAccess, user_id=row.user_id, course_id=row.course_id)):
                await CourseAccess.create(row.user_id, row.course_id)
            row.state = "fulfilled"
            row.fulfillment = {
                "kind": "course_access_provided",
                "course_id": row.course_id,
                "provided_at": datetime.now(timezone.utc).isoformat(),
                "confirmation_smtp_accepted_at": outcome["confirmation_smtp_accepted_at"],
                "order_id": row.id,
                "ledger_id": row.id if row.offer["product"]["coins"] else None,
            }
            await clear_cache("course_access")
    # Transport, 5xx, malformed and auth failures retain the exact prepared command.
    await db.commit()
    if row.state == "fulfilled":
        await report(row)


async def report(row: CoursePurchase) -> None:
    if row.reported or row.fulfillment is None:
        return
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/purchase-fulfillment/skills/{row.user_id}/{row.id}", json=row.fulfillment)
    if response.status_code == 200:
        row.reported = True
        await db.commit()


@db_wrapper
async def recover() -> None:
    ids = [
        r.id
        for r in await db.all(
            filter_by(CoursePurchase)
            .where(
                CoursePurchase.state.in_(["prepared", "paid"])
                | ((CoursePurchase.state == "fulfilled") & CoursePurchase.reported.is_(False))
            )
            .order_by(CoursePurchase.created_at)
        )
    ]
    for order_id in ids:
        try:
            await deliver(order_id)
        except Exception:
            await db.session.rollback()
            logger.exception("Course purchase recovery retained: %s", order_id)
