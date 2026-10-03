"""The protected legacy writer keeps valid bodies and refuses ambiguous ones."""

import json
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from pytest_mock import MockerFixture


@pytest.mark.parametrize(
    "content_type,xp,status",
    [
        ("application/json", 1, 200),
        ("application/json; charset=utf-8", 1, 200),
        ("application/ld+json", 1, 200),
        (None, 1, 422),
        ("text/plain", 1, 422),
        ("application/json", "1", 200),
        ("application/json", 1.0, 200),
        ("application/json", 1.5, 422),
    ],
)
async def test__protected_progress_body_validation(
    auth_client: AsyncClient, mocker: MockerFixture, content_type: str | None, xp: int | float | str, status: int
) -> None:
    mocker.patch("api.endpoints.internal.skills.db.exists", AsyncMock(return_value=True))
    add_xp = mocker.patch("api.endpoints.internal.skills.models.XP.add_xp", AsyncMock())
    mocker.patch("api.endpoints.internal.skills.clear_cache", AsyncMock())
    headers = {"Content-Type": content_type} if content_type else {}
    response = await auth_client.post("/_internal/skills/user/skill", content=json.dumps({"xp": xp}), headers=headers)
    assert response.status_code == status
    if status == 200:
        assert response.json() is True
        add_xp.assert_awaited_once_with("user", "skill", 1)
    else:
        assert isinstance(response.json()["detail"], list)
        add_xp.assert_not_called()
