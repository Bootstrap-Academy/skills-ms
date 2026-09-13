"""Actual local entitlement delivery/admission, with backend authority response stubbed."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context, filter_by
from api.endpoints import course as course_routes
from api.schemas.user import User
from api.services import retained_rights
from api.services.courses import COURSES
from api.services.user_deletion import delete_user_data
from api.utils.utc import utcnow


async def preserved(mocker: MockerFixture, source: str = "source") -> dict[str, Any]:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    mocker.patch.dict(COURSES, {"course": SimpleNamespace(id="course", free=False)})
    async with db_context():
        await db.add(models.LastWatch(user_id=source, course_id="course", timestamp=utcnow()))
    async with db_context():
        await delete_user_data(source)
    async with db_context():
        return (await retained_rights.list_rights(source))[0]


def authority(right: dict[str, Any], subject: str = "fresh") -> dict[str, Any]:
    original = {key: value for key, value in right.items() if key not in ("current_subject", "generation")}
    return {
        "id": str(uuid4()),
        "source": "skills",
        "successor": subject,
        "original_contract": right["id"],
        "original_scope": original,
        "claimant_authorization": {"source_subject": right["source_user_id"]},
        "purpose": "existing_course_continuation",
        "new_purchase": False,
    }


async def test__same_right_delivery_is_usable_and_replay_does_not_resurrect(mocker: MockerFixture) -> None:
    right = await preserved(mocker)
    grant = authority(right)
    response = mocker.patch("api.services.retained_rights.successor_authority", AsyncMock(return_value=grant))
    async with db_context():
        delivered = await retained_rights.deliver("source", grant["id"])
    assert delivered["state"] == "granted" and delivered["original_result"]["new_purchase"] is False
    async with db_context():
        assert await retained_rights.deliver("source", grant["id"]) == delivered
        assert await db.all(filter_by(models.CoursePurchase, user_id="fresh")) == []
        assert await db.all(filter_by(models.LastWatch, user_id="fresh")) == []
    # Actual admission reads committed access through the service's separate
    # admission connection; it must not depend on a second payment or Premium.
    premium = mocker.patch(
        "api.endpoints.course.has_premium", AsyncMock(side_effect=AssertionError("No new Premium needed"))
    )
    async with db_context():
        await course_routes.has_course_access.dependency(
            course=COURSES["course"], user=User(id="fresh", email_verified=True, admin=False)
        )
    premium.assert_not_called()
    async with db_context():
        await delete_user_data("fresh")
    response.reset_mock()
    async with db_context():
        replay = await retained_rights.deliver("source", grant["id"])
        assert replay["state"] == "withdrawn" and replay["original_result"] == delivered["original_result"]
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))
        stored = await db.get(models.RetainedCourseRight, id=right["id"])
        assert stored is not None
        assert retained_rights.original(stored) == grant["original_scope"] and stored.current_subject is None
    response.assert_not_called()
    successor = authority(right, "next-fresh")
    response.return_value = successor
    async with db_context():
        resumed = await retained_rights.deliver("source", successor["id"])
        assert resumed["state"] == "granted"
    async with db_context():
        await course_routes.has_course_access.dependency(
            course=COURSES["course"], user=User(id="next-fresh", email_verified=True, admin=False)
        )
        assert await db.all(filter_by(models.CoursePurchase, user_id="next-fresh")) == []
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))


async def test__changed_or_unavailable_admission_keeps_original_entitlement(mocker: MockerFixture) -> None:
    right = await preserved(mocker)
    grant = authority(right)
    response = mocker.patch("api.services.retained_rights.successor_authority", AsyncMock(side_effect=[grant, None]))
    with pytest.raises(HTTPException) as denied:
        async with db_context():
            await retained_rights.deliver("source", grant["id"])
    assert denied.value.status_code == 409
    async with db_context():
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))
        assert not await db.exists(filter_by(models.CourseRightGrant, id=grant["id"]))
        assert (await retained_rights.list_rights("source"))[0]["current_subject"] is None
    response.side_effect = None
    response.return_value = grant
    with pytest.raises(HTTPException):
        async with db_context():
            await retained_rights.deliver("foreign", grant["id"])
    async with db_context():
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))
        assert (await retained_rights.list_rights("source"))[0]["original"] == right["original"]


async def test__rolled_back_delivery_and_missing_course_do_not_satisfy_right(mocker: MockerFixture) -> None:
    right = await preserved(mocker)
    grant = authority(right)
    mocker.patch("api.services.retained_rights.successor_authority", AsyncMock(return_value=grant))
    with pytest.raises(RuntimeError):
        async with db_context():
            await retained_rights.deliver("source", grant["id"])
            raise RuntimeError("Synthetic local transaction failure before commit")
    async with db_context():
        assert not await db.exists(filter_by(models.CourseAccess, user_id="fresh"))
        assert not await db.exists(filter_by(models.CourseRightGrant, id=grant["id"]))
    mocker.patch.dict(COURSES, {}, clear=True)
    with pytest.raises(HTTPException):
        async with db_context():
            await retained_rights.deliver("source", grant["id"])
    async with db_context():
        assert (await retained_rights.list_rights("source"))[0]["current_subject"] is None
        assert not await db.exists(filter_by(models.CourseRightGrant, id=grant["id"]))
