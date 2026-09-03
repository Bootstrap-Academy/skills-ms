from unittest.mock import AsyncMock

from httpx import AsyncClient
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context, filter_by
from api.utils.utc import utcnow


async def test__delete_user(auth_client: AsyncClient, mocker: MockerFixture) -> None:
    delete_user_data = mocker.patch("api.endpoints.internal.users.delete_user_data", AsyncMock())

    response = await auth_client.delete("/_internal/users/user42")

    assert response.status_code == 204
    assert not response.content
    delete_user_data.assert_called_once_with("user42")


async def test__delete_user__deletes_all_data(auth_client: AsyncClient, mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())

    async with db_context():
        await db.add(models.XP(id="xp", user_id="user", skill_id="sub", xp=42, last_update=utcnow()))

        response = await auth_client.delete("/_internal/users/user")

        assert response.status_code == 204
        assert not await db.exists(filter_by(models.XP, user_id="user"))


async def test__delete_user__unknown_user(auth_client: AsyncClient, mocker: MockerFixture) -> None:
    mocker.patch("api.services.user_deletion.clear_cache", AsyncMock())

    async with db_context():
        response = await auth_client.delete("/_internal/users/user")

    assert response.status_code == 204
