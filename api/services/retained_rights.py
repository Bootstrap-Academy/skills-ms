"""Continuation of evidenced course rights does not create a new purchase."""
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from api.database import db, filter_by, select
from api.models import CourseAccess, CoursePurchase, CourseRightGrant, LastWatch, RetainedCourseRight


async def preserve_before_erasure(user_id: str) -> None:
    """Caller owns the same subject guard as delivery and deletion.

    LastWatch itself is an existing access predicate. Preserve that observation,
    not its viewing timestamp or an invented historical payment/duration.
    """
    # InnoDB REPEATABLE READ may already have a pre-wait snapshot. Current
    # locking reads observe the delivery committed before this subject lock,
    # matching the current-state DELETE that follows in the same transaction.
    access = {row.course_id for row in await db.all(filter_by(CourseAccess, user_id=user_id).with_for_update().execution_options(populate_existing=True))}
    started = {row.course_id for row in await db.all(filter_by(LastWatch, user_id=user_id).with_for_update().execution_options(populate_existing=True))}
    continued = await db.all(filter_by(RetainedCourseRight, current_subject=user_id).with_for_update().execution_options(populate_existing=True))
    continued_courses = {row.course_id for row in continued}
    for right in continued:
        right.current_subject = None
        right.generation += 1
    for grant in await db.all(filter_by(CourseRightGrant, subject=user_id).where(CourseRightGrant.state == "granted").with_for_update().execution_options(populate_existing=True)):
        grant.state = "withdrawn"
    for course_id in sorted((access | started) - continued_courses):
        if await db.first(filter_by(RetainedCourseRight, source_user_id=user_id, course_id=course_id)):
            continue
        purchases = await db.all(filter_by(CoursePurchase, user_id=user_id, course_id=course_id).with_for_update().execution_options(populate_existing=True))
        await db.add(RetainedCourseRight(
            id=str(uuid4()), source_user_id=user_id, course_id=course_id,
            observed_at=datetime.now(timezone.utc), current_subject=None, generation=0,
            original={
                "source_user_id": user_id, "course_id": course_id,
                "observed_course_access": course_id in access,
                "observed_started_course_access": course_id in started,
                "purchase_ids": [row.id for row in purchases],
                "scope": "Existing course admission at erasure; no original payment, performance, duration or new terms inferred",
                "viewing_history_retained": False,
            },
        ))


def original(right: RetainedCourseRight) -> dict[str, Any]:
    return {"id": right.id, "source_user_id": right.source_user_id, "course_id": right.course_id,
            "observed_at": right.observed_at.isoformat(), "original": right.original}


async def list_rights(user_id: str) -> list[dict[str, Any]]:
    return [original(right) | {"current_subject": right.current_subject, "generation": right.generation}
            for right in await db.all(filter_by(RetainedCourseRight, source_user_id=user_id).order_by(RetainedCourseRight.id))]


async def get_original(source_user_id: str, right_id: str) -> dict[str, Any]:
    from fastapi import HTTPException
    right = await db.first(filter_by(RetainedCourseRight, id=right_id, source_user_id=source_user_id))
    if right is None:
        raise HTTPException(404, "Observed course right unavailable for this source")
    return original(right)


async def successor_authority(source_user_id: str, grant_id: str) -> dict[str, Any] | None:
    from fastapi import HTTPException
    from httpx import HTTPError
    from api.services.internal import InternalService
    try:
        async with InternalService.SHOP.client as client:
            client.event_hooks["response"] = []
            response = await client.post("/claims/course_successor_authority", json={
                "grant_id": grant_id, "source_subject": source_user_id,
            })
        if response.status_code != 200:
            raise HTTPException(503, "Current course continuation admission unavailable")
        value = response.json()
        if value is None:
            return None
        if (value.get("id") != grant_id or value.get("source") != "skills"
                or value.get("purpose") != "existing_course_continuation" or value.get("new_purchase") is not False
                or value.get("claimant_authorization", {}).get("source_subject") != source_user_id):
            raise HTTPException(503, "Invalid course continuation admission")
        return value
    except (HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Current course continuation admission unavailable") from None


def delivery_result(grant: CourseRightGrant) -> dict[str, Any]:
    # The immutable original result is distinct from current access after a
    # later erasure. An old delivery replay never recreates deleted access.
    return {"grant_id": grant.id, "right_id": grant.right_id, "subject": grant.subject,
            "state": grant.state, "original_result": grant.result, "new_purchase": False}


async def deliver(source_user_id: str, grant_id: str) -> dict[str, Any]:
    from fastapi import HTTPException
    from api.models import PurchaseUser
    from api.services import purchases
    from api.services.courses import COURSES

    prior = await db.first(filter_by(CourseRightGrant, id=grant_id))
    if prior is not None:
        # Read exact previous delivery through the established source owner,
        # even after its target was erased or its learning key expired.
        await get_original(source_user_id, prior.right_id)
        await purchases.lock_user(prior.subject)
        prior = await db.first(filter_by(CourseRightGrant, id=grant_id).with_for_update().execution_options(populate_existing=True))
        assert prior is not None
        return delivery_result(prior)
    authority = await successor_authority(source_user_id, grant_id)
    if authority is None:
        raise HTTPException(409, "Current continuation election unavailable; original rights remain")
    subject = str(authority["successor"])
    guard = await purchases.lock_user(subject)
    prior = await db.first(filter_by(CourseRightGrant, id=grant_id).with_for_update().execution_options(populate_existing=True))
    if prior is not None:
        await get_original(source_user_id, prior.right_id)
        return delivery_result(prior)
    if guard.deleted:
        raise HTTPException(409, "Target learning data was erased; original rights remain")
    current = await successor_authority(source_user_id, grant_id)
    if current is None or current["successor"] != subject or current["original_scope"] != authority["original_scope"]:
        raise HTTPException(409, "Continuation admission changed while waiting")
    right = await db.first(filter_by(RetainedCourseRight, id=authority["original_contract"], source_user_id=source_user_id).with_for_update().execution_options(populate_existing=True))
    source_guard = await db.first(filter_by(PurchaseUser, user_id=source_user_id))
    if right is None or source_guard is None or not source_guard.deleted or original(right) != authority["original_scope"]:
        raise HTTPException(409, "Exact preserved course admission required")
    if right.current_subject not in (None, subject):
        raise HTTPException(409, "Existing course admission is already in use; original right remains")
    if right.course_id not in COURSES:
        raise HTTPException(409, "Course supply requires resolution; original right remains")
    if not await db.first(filter_by(CourseAccess, user_id=subject, course_id=right.course_id)):
        await db.add(CourseAccess(user_id=subject, course_id=right.course_id))
    right.current_subject = subject
    grant = CourseRightGrant(
        id=grant_id, right_id=right.id, subject=subject,
        request={"source_subject": source_user_id, "original_scope": authority["original_scope"],
                 "backend_grant_id": grant_id, "successor": subject},
        state="granted", created_at=datetime.now(timezone.utc),
        result={"course_id": right.course_id, "access_granted": True, "new_purchase": False,
                "new_terms_accepted": False, "original_performance_inferred": False,
                "original_scope": authority["original_scope"]},
    )
    await db.add(grant)
    return delivery_result(grant)
