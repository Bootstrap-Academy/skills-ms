"""Private course-wide project state per learner, shared by every lesson of one course.

Admission is exactly the check for a lesson opened in that course. The state never awards XP,
completes a lesson or changes a course; it is erased with the account.
"""

import json
from hashlib import sha256
from typing import cast

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from api.database import db, delete, filter_by
from api.models import CourseProject, CourseProjectRequest, PurchaseUser
from api.schemas.course_project import PROJECT_STATE_LIMIT, ProjectEnvelope, SaveProject, state_size
from api.schemas.user import User
from api.services import rooms
from api.services.purchases import lock_user
from api.utils.utc import utcnow

# Receipts hold no state, so exact retries of more than the latest save stay cheap to answer.
KEPT_RECEIPTS = 16


def envelope(course_id: str, row: CourseProject | None) -> ProjectEnvelope:
    if row is None:
        return ProjectEnvelope(course_id=course_id, revision=0, state={}, updated_at=None)
    return ProjectEnvelope(course_id=course_id, revision=row.revision, state=row.state, updated_at=row.updated_at)


def request_fingerprint(course_id: str, data: SaveProject) -> str:
    payload = {"course_id": course_id, "expected_revision": data.expected_revision, "state": data.state}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def get_project(course_id: str, user: User) -> ProjectEnvelope:
    # Reads never create the durable user lock or a project row.
    guard = await db.get(PurchaseUser, user_id=user.id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    await rooms.require_course_lessons(course_id, user)
    row = await db.first(
        filter_by(CourseProject, user_id=user.id, course_id=course_id).execution_options(populate_existing=True)
    )
    return envelope(course_id, row)


async def save_project(course_id: str, user: User, data: SaveProject) -> ProjectEnvelope:
    # The erasure lock, as for room states: a save admitted before erasure cannot recreate the project.
    guard = await lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    await rooms.require_course_lessons(course_id, user)
    if state_size(data.state) > PROJECT_STATE_LIMIT:
        raise HTTPException(413, "The project is larger than 64 KiB")
    fingerprint = request_fingerprint(course_id, data)
    receipt = await db.get(CourseProjectRequest, user_id=user.id, request_id=str(data.request_id))
    if receipt is not None:
        if receipt.fingerprint != fingerprint:
            raise HTTPException(409, "This request was already used for another change")
        # Same fingerprint, so the retried body carries exactly the state that was saved.
        return ProjectEnvelope(
            course_id=course_id, revision=receipt.revision, state=data.state, updated_at=receipt.updated_at
        )
    row = await db.first(
        filter_by(CourseProject, user_id=user.id, course_id=course_id).execution_options(populate_existing=True)
    )
    current = envelope(course_id, row)
    if current.revision != data.expected_revision:
        raise HTTPException(409, "Your project has changed in another session")
    now = utcnow()
    saved = ProjectEnvelope(course_id=course_id, revision=current.revision + 1, state=data.state, updated_at=now)
    if row is None:
        await db.add(
            CourseProject(
                user_id=user.id, course_id=course_id, revision=saved.revision, state=saved.state, updated_at=now
            )
        )
    else:
        result = await db.exec(
            update(CourseProject)
            .where(
                CourseProject.user_id == user.id,
                CourseProject.course_id == course_id,
                CourseProject.revision == data.expected_revision,
            )
            .values(revision=saved.revision, state=saved.state, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise HTTPException(409, "Your project has changed in another session")
    await db.add(
        CourseProjectRequest(
            user_id=user.id,
            request_id=str(data.request_id),
            course_id=course_id,
            revision=saved.revision,
            fingerprint=fingerprint,
            updated_at=now,
        )
    )
    try:
        await db.session.flush()
    except IntegrityError:
        # The user lock serializes this on PostgreSQL/MySQL; the keys also protect databases without row locks.
        await db.session.rollback()
        raise HTTPException(409, "Your project has changed in another session") from None
    keep = await db.all(
        filter_by(CourseProjectRequest, user_id=user.id, course_id=course_id)
        .order_by(CourseProjectRequest.revision.desc())
        .limit(KEPT_RECEIPTS)
    )
    await db.exec(
        delete(CourseProjectRequest).where(
            CourseProjectRequest.user_id == user.id,
            CourseProjectRequest.course_id == course_id,
            CourseProjectRequest.request_id.notin_([receipt.request_id for receipt in keep]),
        )
    )
    return saved
