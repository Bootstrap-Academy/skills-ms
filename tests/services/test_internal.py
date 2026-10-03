import ssl
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from _pytest.monkeypatch import MonkeyPatch
from pytest_mock import MockerFixture

from api.services.internal import InternalService, InternalServiceError, client_ssl_context
from api.settings import settings
from api.utils.jwt import decode_jwt


async def test__internal_service__get_token(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    encode_jwt = mocker.patch("api.services.internal.encode_jwt")
    monkeypatch.setattr(settings, "internal_jwt_ttl", 123)
    monkeypatch.setattr(settings, "jwt_secret", "the shared secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "the auth secret")
    service = MagicMock()
    service.name = "AUTH"

    result = InternalService._get_token(service)

    encode_jwt.assert_called_once_with({"aud": "auth"}, timedelta(seconds=123), secret="the auth secret")
    assert result == encode_jwt()


async def test__internal_service__get_token__falls_back_to_the_shared_secret(
    mocker: MockerFixture, monkeypatch: MonkeyPatch
) -> None:
    encode_jwt = mocker.patch("api.services.internal.encode_jwt")
    monkeypatch.setattr(settings, "internal_jwt_ttl", 123)
    monkeypatch.setattr(settings, "jwt_secret", "the shared secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "")
    service = MagicMock()
    service.name = "AUTH"

    result = InternalService._get_token(service)

    encode_jwt.assert_called_once_with({"aud": "auth"}, timedelta(seconds=123), secret="the shared secret")
    assert result == encode_jwt()


@pytest.mark.parametrize(
    "code,ok",
    [(200, True), (201, True), (401, False), (403, False), (404, True), (500, False), (501, False), (502, False)],
)
async def test__internal_service__handle_error(code: int, ok: bool) -> None:
    response = AsyncMock(status_code=code, text="response text asdf")

    if ok:
        await InternalService._handle_error(response)
        response.aread.assert_not_called()
    else:
        with pytest.raises(InternalServiceError) as e:
            await InternalService._handle_error(response)
        response.aread.assert_called_once_with()
        assert e.value.args == (response, "response text asdf")


async def test__internal_service__client(mocker: MockerFixture, monkeypatch: MonkeyPatch) -> None:
    async_client = mocker.patch("api.services.internal.AsyncClient")
    service = MagicMock(value="http://example.service:1234/test/")

    result = InternalService.client.fget(service)  # type: ignore

    async_client.assert_called_once()
    args = async_client.call_args[1]
    assert result == async_client()

    assert args["base_url"] == "http://example.service:1234/test/_internal"
    assert args["headers"] == {"Authorization": service._get_token()}

    event_hooks = args["event_hooks"]
    assert [*event_hooks] == ["response"]
    assert event_hooks["response"] == [service._handle_error]


async def test_challenges_client_uses_bearer_and_its_own_audience(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "internal_jwt_secret_challenges", "synthetic challenge audience secret")
    async with InternalService.CHALLENGES.client as client:
        header = client.headers["Authorization"]
        assert header.startswith("Bearer ")
        token = header.removeprefix("Bearer ")
        assert decode_jwt(token, audience=["challenges"], secret=settings.internal_jwt_secret_challenges)
        assert decode_jwt(token, audience=["skills"], secret=settings.internal_jwt_secret_challenges) is None


async def test_internal_clients_reuse_verified_trust_without_reusing_authority(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "internal_jwt_secret_challenges", "first synthetic audience key")
    async with InternalService.CHALLENGES.client as first:
        first_token = first.headers["Authorization"].removeprefix("Bearer ")
    monkeypatch.setattr(settings, "internal_jwt_secret_challenges", "second synthetic audience key")
    async with InternalService.CHALLENGES.client as second:
        second_token = second.headers["Authorization"].removeprefix("Bearer ")
    assert decode_jwt(first_token, audience=["challenges"], secret="first synthetic audience key")
    assert decode_jwt(second_token, audience=["challenges"], secret="second synthetic audience key")
    assert decode_jwt(second_token, audience=["challenges"], secret="first synthetic audience key") is None
    assert client_ssl_context() is client_ssl_context()
    assert client_ssl_context().verify_mode == ssl.CERT_REQUIRED
    assert client_ssl_context().check_hostname is True
