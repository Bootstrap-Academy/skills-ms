"""Invalid numbers and Unicode remain client errors on the real protected write routes."""

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch
from pytest_mock import MockerFixture

from api.app import app
from api.auth import internal_auth, user_auth
from api.schemas.user import User
from api.settings import settings

REQUEST_ID = "a0000000-0000-0000-0000-000000000001"
XP_OPERATION = f"/_internal/xp-operations/{REQUEST_ID}/{REQUEST_ID}/skill"


@pytest.fixture
async def validation_client(monkeypatch: MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setitem(
        app.dependency_overrides, user_auth.dependency, lambda: User(id="synthetic", email_verified=True, admin=False)
    )
    monkeypatch.setitem(app.dependency_overrides, internal_auth.dependency, lambda: {})
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://validation.synthetic",
        headers={"Authorization": "Bearer synthetic", "Content-Type": "application/json"},
    ) as client:
        yield client


@pytest.mark.parametrize(
    "revision,state,extra,field",
    [
        ("0", '{"nested":[{"x":NaN}]}', "", "state"),
        ("0", '{"x":Infinity}', "", "state"),
        ("0", '{"x":-Infinity}', "", "state"),
        ("0", '{"x":1e400}', "", "state"),
        ("NaN", json.dumps({}), "", "expected_revision"),
        ("Infinity", json.dumps({}), "", "expected_revision"),
        ("0", json.dumps({}), ',"extra":{"nested":[Infinity]}', "extra"),
    ],
)
async def test_nonfinite_room_write_is_422_without_mutation(
    validation_client: AsyncClient, mocker: MockerFixture, revision: str, state: str, extra: str, field: str
) -> None:
    mutate = mocker.patch("api.services.rooms.mutate_room", AsyncMock())
    body = f'{{"request_id":"{REQUEST_ID}","expected_revision":{revision},"state":{state}{extra}}}'
    response = await validation_client.put("/rooms/intro/state", content=body)
    mutate.assert_not_called()
    assert response.status_code == 422
    errors = json.loads(response.text, parse_constant=lambda value: pytest.fail(f"Non-JSON error value: {value}"))[
        "detail"
    ]
    assert any(error["loc"] == ["body", field] for error in errors)


@pytest.mark.parametrize("path", ["/_internal/skills/user/skill", XP_OPERATION])
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "1e400"])
async def test_nonfinite_xp_write_is_422_without_award(
    validation_client: AsyncClient, mocker: MockerFixture, path: str, amount: str
) -> None:
    add_xp = mocker.patch("api.endpoints.internal.skills.models.XP.add_xp", AsyncMock())
    apply_xp = mocker.patch("api.endpoints.internal.skills.apply_xp", AsyncMock())
    earning = f',"earning_id":"{REQUEST_ID}"' if path == XP_OPERATION else ""
    response = await validation_client.post(path, content=f'{{"xp":{amount}{earning}}}')
    add_xp.assert_not_called()
    apply_xp.assert_not_called()
    assert response.status_code == 422
    errors = json.loads(response.text, parse_constant=lambda value: pytest.fail(f"Non-JSON error value: {value}"))[
        "detail"
    ]
    assert any(error["loc"] == ["body", "xp"] for error in errors)


@pytest.mark.parametrize("state", ['{"x":NaN}', r'{"x":"\ud800"}', r'{"\udfff":{"nested":["ok"]}}'])
async def test_invalid_project_state_is_422_without_mutation(
    validation_client: AsyncClient, mocker: MockerFixture, state: str
) -> None:
    save = mocker.patch("api.services.course_project.save_project", AsyncMock())
    body = f'{{"request_id":"{REQUEST_ID}","expected_revision":0,"state":{state}}}'
    response = await validation_client.put("/courses/synthetic/project", content=body)
    save.assert_not_called()
    assert response.status_code == 422
    assert any(error["loc"] == ["body", "state"] for error in response.json()["detail"])


async def test_non_utf8_body_is_a_client_error_without_award(
    validation_client: AsyncClient, mocker: MockerFixture
) -> None:
    add_xp = mocker.patch("api.endpoints.internal.skills.models.XP.add_xp", AsyncMock())
    response = await validation_client.post(
        "/_internal/skills/user/skill", content=b"\xff", headers={"Content-Type": "text/plain"}
    )
    add_xp.assert_not_called()
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


async def test_valid_numbers_still_reach_room_and_xp_writers(
    validation_client: AsyncClient, mocker: MockerFixture
) -> None:
    mutate = mocker.patch(
        "api.services.rooms.mutate_room",
        AsyncMock(
            return_value={
                "unit": {
                    "id": "intro",
                    "path_id": "synthetic",
                    "title": {"de": "Synthetic", "en": "Synthetic"},
                    "room": "custom",
                    "content": {},
                    "teaches": [],
                    "practices": [],
                    "requires": [],
                },
                "progress": {"revision": 1, "state": {}, "status": "new", "result": None},
            }
        ),
    )
    mocker.patch("api.endpoints.internal.skills.db.exists", AsyncMock(return_value=True))
    mocker.patch("api.endpoints.internal.skills.db.commit", AsyncMock())
    mocker.patch("api.endpoints.internal.skills.clear_cache", AsyncMock())
    add_xp = mocker.patch("api.endpoints.internal.skills.models.XP.add_xp", AsyncMock())
    apply_xp = mocker.patch("api.endpoints.internal.skills.apply_xp", AsyncMock(return_value={"applied": True}))
    state = {"nested": [None, True, 1.5, "draft"]}
    room = await validation_client.put(
        "/rooms/intro/state", json={"request_id": REQUEST_ID, "expected_revision": 0, "state": state}
    )
    legacy = await validation_client.post("/_internal/skills/user/skill", json={"xp": 1})
    keyed = await validation_client.post(XP_OPERATION, json={"xp": 1, "earning_id": REQUEST_ID})
    assert room.status_code == legacy.status_code == keyed.status_code == 200
    mutate.assert_awaited_once()
    assert mutate.await_args is not None
    assert mutate.await_args.args[3].state == state
    add_xp.assert_awaited_once_with("user", "skill", 1)
    apply_xp.assert_awaited_once()
