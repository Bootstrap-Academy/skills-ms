"""Owning receipt/effect controls with only physical backend status stubbed."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context, filter_by
from api.services.benefits import XPAward, apply_xp
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data


async def seed(mocker: MockerFixture) -> tuple[str, str, XPAward]:
    mocker.patch("api.services.benefits.get_user_status", AsyncMock(return_value=200))
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await db.add(models.SubSkill(id="skill", name="Skill"))
    return str(uuid4()), str(uuid4()), XPAward(xp=17, earning_id=uuid4())


async def test__reply_loss_replay_and_erasure_preserve_receipt_without_new_xp(mocker: MockerFixture) -> None:
    operation, user, award = await seed(mocker)
    async with db_context():
        result = await apply_xp(operation, user, "skill", award)
    async with db_context():
        assert await apply_xp(operation, user, "skill", award) == result
        assert await models.XP.get_user_skill_xp(user, "skill") == 17
    async with db_context():
        await delete_user_data(user)
    unavailable = mocker.patch(
        "api.services.benefits.get_user_status", AsyncMock(side_effect=AssertionError("Exact receipt first"))
    )
    async with db_context():
        assert await apply_xp(operation, user, "skill", award) == result
        assert await models.XP.get_user_skill_xp(user, "skill") == 0
        export = await export_user_data(user)
        assert export.xp_operations[0]["result"] == result
        assert (await export_user_data("foreign")).xp_operations == []
    unavailable.assert_not_called()


async def test__failed_local_transaction_commits_neither_receipt_nor_effect(mocker: MockerFixture) -> None:
    operation, user, award = await seed(mocker)
    with pytest.raises(RuntimeError):
        async with db_context():
            # SQLite's legacy driver otherwise releases a first savepoint as
            # a top-level transaction. This explicitly models the SQL transaction.
            await db.exec("BEGIN")
            await apply_xp(operation, user, "skill", award)
            raise RuntimeError("Synthetic failure before owning commit")
    async with db_context():
        assert not await db.exists(filter_by(models.XPOperation, id=operation))
        assert await models.XP.get_user_skill_xp(user, "skill") == 0
        result = await apply_xp(operation, user, "skill", award)
        assert result["applied"] is True
    async with db_context():
        assert await models.XP.get_user_skill_xp(user, "skill") == 17


async def test__changed_payload_and_remote_unavailability_do_not_apply_or_invent_erasure(mocker: MockerFixture) -> None:
    operation, user, award = await seed(mocker)
    async with db_context():
        await apply_xp(operation, user, "skill", award)
    with pytest.raises(HTTPException) as conflict:
        async with db_context():
            await apply_xp(operation, user, "skill", XPAward(xp=18, earning_id=award.earning_id))
    assert conflict.value.status_code == 409
    status = mocker.patch("api.services.benefits.get_user_status", AsyncMock(return_value=404))
    missing = str(uuid4())
    with pytest.raises(HTTPException) as unavailable:
        async with db_context():
            await db.exec("BEGIN")
            await apply_xp(missing, "unavailable", "skill", award)
    assert unavailable.value.status_code == 503
    async with db_context():
        assert not await db.exists(filter_by(models.XPOperation, id=missing))
        assert await models.XP.get_user_skill_xp("unavailable", "skill") == 0
    status.return_value = 200
    async with db_context():
        assert (await apply_xp(missing, "unavailable", "skill", award))["state"] == "applied"
    async with db_context():
        assert await models.XP.get_user_skill_xp(user, "skill") == 17
        assert await models.XP.get_user_skill_xp("unavailable", "skill") == 17


async def test__local_erasure_marker_is_distinct_from_remote_unavailability(mocker: MockerFixture) -> None:
    operation, user, award = await seed(mocker)
    async with db_context():
        await delete_user_data(user)
    status = mocker.patch(
        "api.services.benefits.get_user_status", AsyncMock(side_effect=AssertionError("Local marker owns this fact"))
    )
    async with db_context():
        result = await apply_xp(operation, user, "skill", award)
        assert result["state"] == "recipient_erased" and result["applied"] is False
    async with db_context():
        assert await apply_xp(operation, user, "skill", award) == result
        assert await models.XP.get_user_skill_xp(user, "skill") == 0
    status.assert_not_called()


async def test__zero_distinct_awards_and_unkeyed_writer_share_current_counter(mocker: MockerFixture) -> None:
    operation, user, award = await seed(mocker)
    async with db_context():
        assert (await apply_xp(operation, user, "skill", XPAward(xp=0, earning_id=award.earning_id)))["applied"]
    async with db_context():
        await apply_xp(str(uuid4()), user, "skill", award)
        await models.XP.add_xp(user, "skill", 3)
    async with db_context():
        assert await models.XP.get_user_skill_xp(user, "skill") == 20
