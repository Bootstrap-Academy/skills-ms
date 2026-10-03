"""The bounded read batch retains the single-check contract and never starts."""

from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
from httpx import AsyncClient
from pytest_mock import MockerFixture

from api.database import db_context
from api.services import daily_limit


async def test_read_batch_preserves_order_and_individual_denials(
    auth_client: AsyncClient, mocker: MockerFixture
) -> None:
    admission = mocker.patch(
        "api.services.daily_limit.challenge_admission",
        AsyncMock(side_effect=[{"allowed": True}, HTTPException(403), HTTPException(404), {"allowed": True}]),
    )
    task = str(uuid4())
    requests = [{"task_id": task, "subtask_id": str(uuid4())} for _ in range(4)]
    async with db_context():
        response = await auth_client.post("/_internal/learning-access/reader/check-batch", json={"requests": requests})
    assert response.status_code == 200 and response.json() == {"readable": [True, False, False, True]}
    assert [str(call.args[1].subtask_id) for call in admission.await_args_list] == [r["subtask_id"] for r in requests]
    assert all(call.args[0] == "reader" and call.args[2] is False for call in admission.await_args_list)


async def test_read_batch_does_not_mask_outages(auth_client: AsyncClient, mocker: MockerFixture) -> None:
    admission = mocker.patch(
        "api.services.daily_limit.challenge_admission",
        AsyncMock(side_effect=daily_limit.AccessError(503, "learning_access_unavailable", "unavailable")),
    )
    response = await auth_client.post("/_internal/learning-access/reader/check-batch", json={"requests": [{}]})
    assert response.status_code == 503
    admission.assert_awaited_once()


async def test_read_batch_rejects_starts_and_unbounded_payloads(
    auth_client: AsyncClient, mocker: MockerFixture
) -> None:
    admission = mocker.patch("api.services.daily_limit.challenge_admission", AsyncMock(return_value={"allowed": True}))
    for requests in ([], [{}] * 251, [{"request_id": str(uuid4())}], [{"user_admin": "false"}]):
        response = await auth_client.post("/_internal/learning-access/reader/check-batch", json={"requests": requests})
        assert response.status_code == 422
    admission.assert_not_awaited()
    response = await auth_client.post("/_internal/learning-access/reader/check-batch", json={"requests": [{}] * 250})
    assert response.status_code == 200 and response.json() == {"readable": [True] * 250}


async def test_read_batch_requires_internal_auth(client: AsyncClient) -> None:
    response = await client.post("/_internal/learning-access/reader/check-batch", json={"requests": [{}]})
    assert response.status_code == 401
