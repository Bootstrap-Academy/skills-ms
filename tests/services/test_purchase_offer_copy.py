"""New offer wording must not replace a customer's stored purchase evidence."""

import copy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api.database import db, db_context
from api.models.purchase import CoursePurchase
from api.schemas.course import Course
from api.services import purchases
from api.utils.docs import get_example


@pytest.mark.parametrize("state", ["prepared", "paid", "review"])
async def test_pending_order_keeps_original_offer(state: str, mocker: MockerFixture) -> None:
    course = Course(**{**get_example(Course), "id": "synthetic-course", "price": 100})
    original: dict[str, Any] = {
        "id": str(uuid4()),
        "hash": "original-offer-hash",
        "text": "Unverändertes früheres Angebot: 100 MorphCoins.",
        "product": {"description": "Frühere Angebotsbeschreibung", "coins": 100},
    }
    acceptance = {"offer_hash": original["hash"], "accepted": True}
    saved = copy.deepcopy(original)
    async with db_context():
        await db.add(
            CoursePurchase(
                id=original["id"],
                user_id="synthetic-user",
                course_id=course.id,
                state=state,
                active_key=f"synthetic-user:{course.id}",
                offer=original,
                acceptance=acceptance,
                created_at=datetime.now(timezone.utc),
            )
        )
    # A pending order must not be re-quoted against today's copy or contact SHOP.
    mocker.patch.object(purchases, "product", side_effect=AssertionError("Must use stored offer"))
    async with db_context():
        result = await purchases.offer("synthetic-user", course)
        assert result["offer"] == saved
        assert result["state"] == state
    async with db_context():
        row = await db.get(CoursePurchase, id=original["id"])
        assert row is not None
        assert row.offer == saved
        assert row.acceptance == acceptance
        assert row.state == state


async def test_unaccepted_old_wording_cannot_be_silently_replaced() -> None:
    course = Course(**{**get_example(Course), "id": "synthetic-course", "price": 100})
    original_product = purchases.product(course)
    original_product["description"] = "Frühere Angebotsbeschreibung"
    order_id = uuid4()
    original: dict[str, Any] = {"id": str(order_id), "hash": "original-offer-hash", "product": original_product}
    async with db_context():
        await db.add(
            CoursePurchase(
                id=str(order_id),
                user_id="synthetic-user",
                course_id=course.id,
                state="offered",
                offer=original,
                created_at=datetime.now(timezone.utc),
            )
        )
    async with db_context():
        with pytest.raises(HTTPException) as error:
            await purchases.buy(
                "synthetic-user",
                course,
                purchases.Acceptance(
                    order_id=order_id, offer_hash=original["hash"], accepted=True, early_performance_requested=True
                ),
            )
        assert error.value.status_code == 409
    async with db_context():
        row = await db.get(CoursePurchase, id=str(order_id))
        assert row is not None
        assert row.offer == original
        assert row.acceptance is None
        assert row.state == "offered"
