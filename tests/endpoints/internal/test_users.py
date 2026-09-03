from unittest.mock import AsyncMock

from httpx import AsyncClient
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context, filter_by
from api.schemas.user_export import UserDataExport
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


async def test__export_user(auth_client: AsyncClient, mocker: MockerFixture) -> None:
    export = UserDataExport(course_access=[], last_watch=[], lecture_progress=[], sub_skill_bookmarks=[], xp=[])
    export_user_data = mocker.patch("api.endpoints.internal.users.export_user_data", AsyncMock(return_value=export))

    response = await auth_client.get("/_internal/users/user42/export")

    assert response.status_code == 200
    assert response.json() == export.dict()
    export_user_data.assert_called_once_with("user42")


async def test__export_user__returns_the_data_of_the_user(auth_client: AsyncClient) -> None:
    async with db_context():
        await db.add(models.XP(id="xp", user_id="user", skill_id="sub", xp=42, last_update=utcnow()))
        await db.add(models.XP(id="xp2", user_id="other_user", skill_id="sub", xp=1337, last_update=utcnow()))

        response = await auth_client.get("/_internal/users/user/export")

    assert response.status_code == 200
    assert [xp["xp"] for xp in response.json()["xp"]] == [42]


async def test__export_user__unknown_user(auth_client: AsyncClient) -> None:
    async with db_context():
        response = await auth_client.get("/_internal/users/user/export")

    assert response.status_code == 200
    assert response.json() == {
        "course_access": [],
        "last_watch": [],
        "lecture_progress": [],
        "sub_skill_bookmarks": [],
        "xp": [],
    }
