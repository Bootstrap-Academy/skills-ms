"""Contract with a running llm-ms (28d8ee9 or later): its grants come from here, its verdicts end here.

Skipped unless `LLM_CONTRACT_URL` names a local llm-ms (`http://127.0.0.1:<port>`). It also needs
`LLM_CONTRACT_JWT_SECRET_FILE` (llm-ms `auth.jwt_secret`, to mint access tokens like the backend does),
`LLM_CONTRACT_GRANT_SECRET_FILE` (the grant key file both services read),
`LLM_CONTRACT_VERDICT_SECRET_FILE` (the verdict key file both services read) and
`LLM_CONTRACT_GRADING_PROFILE` (the grading profile file llm-ms loaded, pinned by its SHA-256 here).

The gateway's `/health` says its provider mode and the environment its verdicts name (`env`); this service
is configured with that environment. In `fake` mode llm-ms signs no verdict (`receipt: null`); only the
grant direction, the null receipt and the fallback without the model are checked. Signed verdicts need
`live` mode pointed at llm-ms' own fake OpenAI server (`academy-llm-testing --control-tokens` on a local
port, `providers.openai.allow_unofficial_base_url`, an environment other than `prod`), exactly as the
llm-ms integration tests do. Nothing here can reach a paid API: the test only talks to 127.0.0.1.
"""

import os
from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from time import time
from typing import Any, AsyncIterator, Callable
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from pytest import MonkeyPatch

from api import models
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints.rooms import router
from api.exceptions.api_exception import CodedAPIException
from api.schemas.course import Course
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import llm, rooms
from api.settings import settings


URL = os.environ.get("LLM_CONTRACT_URL", "")
OTHER_PROFILE = os.environ.get("LLM_CONTRACT_OTHER_PROFILE", "llmb-temperature-fan")
ANSWER = "Du bist Reiseleiter. Plane mir drei Tage in Rom. Antworte als Tabelle mit Uhrzeiten."
FAILING_ANSWER = "Rom? [[fake:fail]]"

pytestmark = pytest.mark.skipif(not URL, reason="LLM_CONTRACT_URL is not set (no local llm-ms)")


class Gateway:
    def __init__(
        self, client: httpx.AsyncClient, jwt_secret: bytes, profile: str, mode: str, environment: str | None
    ) -> None:
        self.client = client
        self.jwt_secret = jwt_secret
        self.profile = profile
        self.mode = mode
        self.environment = environment

    def require(self, mode: str) -> None:
        if self.mode != mode:
            pytest.skip(f"needs llm-ms in {mode} mode, this one runs {self.mode}")

    def token(self, user_id: str) -> str:
        # The backend's access-token shape, as llm-ms and this service verify it.
        claims = {
            "uid": user_id,
            "rt": uuid4().hex,
            "sid": str(uuid4()),
            "data": {"email_verified": True, "admin": False, "mfa": False},
            "exp": int(time()) + 300,
        }
        return jwt.encode(claims, self.jwt_secret, algorithm="HS256")

    async def profile_info(self, user_id: str, grant: str, profile: str | None = None) -> httpx.Response:
        return await self.client.get(
            f"/v1/profiles/{profile or self.profile}",
            headers={"Authorization": f"Bearer {self.token(user_id)}", "X-LLM-Grant": grant},
        )

    async def grade(self, user_id: str, grant: str, answer: str, locale: str = "de") -> dict[str, Any]:
        request_id = str(uuid4())
        response = await self.client.post(
            "/v1/respond",
            json={
                "request_id": request_id,
                "grant": grant,
                "profile": self.profile,
                "locale": locale,
                "input": [{"role": "user", "content": answer}],
            },
            headers={"Authorization": f"Bearer {self.token(user_id)}", "Accept": "application/json"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["request_id"] == request_id and body["status"] == "completed"
        output = body["outputs"][0]
        assert output["type"] == "grading", output
        return {"request_id": request_id, **output["grading"]}


def tamper(token: str, change: Callable[[dict[str, Any]], None]) -> str:
    header, body, signature = token.split(".")
    claims = loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    change(claims)
    return f"{header}.{urlsafe_b64encode(dumps(claims).encode()).decode().rstrip('=')}.{signature}"


def flip_signature(token: str) -> str:
    head, _, signature = token.rpartition(".")
    return f"{head}.{('A' if signature[0] != 'A' else 'B')}{signature[1:]}"


def env_file(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for the llm-ms contract test")
    return Path(value)


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    # Never anything but a local gateway.
    assert URL.startswith("http://127.0.0.1:"), URL
    jwt_secret = env_file("LLM_CONTRACT_JWT_SECRET_FILE").read_bytes().rstrip(b"\r\n")
    profile = loads(env_file("LLM_CONTRACT_GRADING_PROFILE").read_bytes())["id"]
    async with httpx.AsyncClient(base_url=URL, timeout=20, trust_env=False) as client:
        health = await client.get("/health")
        assert health.status_code == 200, health.text
        mode = health.json()["provider_mode"]
        assert mode in ("fake", "live"), health.text
        environment = health.json()["environment"]
        # Live verdicts always name their environment; a local fake endpoint can never sign for `prod`.
        assert mode == "fake" or (isinstance(environment, str) and environment != "prod"), health.text
        yield Gateway(client, jwt_secret, profile, mode, environment)


@pytest.fixture
def users() -> dict[str, str]:
    return {"Bearer a": str(uuid4()), "Bearer b": str(uuid4())}


@pytest.fixture
async def skills(monkeypatch: MonkeyPatch, users: dict[str, str], gateway: Gateway) -> AsyncIterator[httpx.AsyncClient]:
    profile_file = env_file("LLM_CONTRACT_GRADING_PROFILE")
    profile = loads(profile_file.read_bytes())["id"]
    grading = {
        "kind": "llm-verdict",
        "profile": profile,
        "profile_sha256": sha256(profile_file.read_bytes()).hexdigest(),
    }

    def unit(uid: str) -> dict[str, Any]:
        return {
            "id": uid,
            "path_id": "prompting",
            "title": {"de": uid, "en": uid},
            "room": "guided-lesson",
            "content": {"de": {"text": "Synthetic"}},
            "teaches": [],
            "practices": [],
            "requires": [],
            "retired": False,
            "llm_profiles": [profile],
            "completion": grading,
        }

    catalogue = Catalogue.model_validate(
        {
            "paths": [{"id": "prompting", "title": {"de": "Pfad", "en": "Path"}, "units": ["graded", "graded-too"]}],
            "units": [unit("graded"), unit("graded-too")],
        }
    )
    course = Course(
        id="prompting-course",
        title="Course",
        description=None,
        category=None,
        language="de",
        image=None,
        authors=[],
        price=0,
        learning_goals=[],
        requirements=[],
        last_update=0,
        learning_path_id="prompting",
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    # The same credential files llm-ms reads.
    monkeypatch.setattr(settings, "llm_grant_secret", "")
    monkeypatch.setattr(settings, "llm_grant_secret_file", env_file("LLM_CONTRACT_GRANT_SECRET_FILE"))
    monkeypatch.setattr(settings, "llm_verdict_secret", "")
    monkeypatch.setattr(settings, "llm_verdict_secret_file", env_file("LLM_CONTRACT_VERDICT_SECRET_FILE"))
    # This host's environment is the one the gateway grades for (fake mode names none).
    monkeypatch.setattr(settings, "llm_verdict_env", gateway.environment or "test")
    monkeypatch.setattr(rooms, "load_catalogue", lambda: catalogue)
    monkeypatch.setattr(rooms, "COURSES", {course.id: course})
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))

    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        token = request.headers.get("Authorization", "")
        if token not in users:
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=users[token], email_verified=True, admin=False)

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = identity
    app.add_exception_handler(CodedAPIException, lambda _, exc: exc.response())
    app.include_router(router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://skills.synthetic",
        headers={"Authorization": "Bearer a"},
    ) as client:
        yield client


async def grant_for(skills: httpx.AsyncClient, unit: str = "graded", **kwargs: Any) -> str:
    response = await skills.post(f"/rooms/{unit}/llm-grant", **kwargs)
    assert response.status_code == 200, response.text
    return str(response.json()["grant"])


def completion(verdict: str | None, answer: str = ANSWER, revision: int = 0, **values: Any) -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "expected_revision": revision,
        "action": "complete",
        "answer": {"text": answer},
        "verdict": verdict,
        **values,
    }


async def test_llm_ms_accepts_grants_from_skills_ms_and_refuses_tampered_ones(
    gateway: Gateway, skills: httpx.AsyncClient, users: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    user = users["Bearer a"]
    grant = await grant_for(skills)
    info = await gateway.profile_info(user, grant)
    assert info.status_code == 200, info.text
    assert info.json()["output"]["type"] == "grading"

    def refused(response: httpx.Response, code: str) -> None:
        assert response.status_code == 403, response.text
        assert response.json()["code"] == code

    refused(await gateway.profile_info(user, flip_signature(grant)), "grant_invalid")
    refused(
        await gateway.profile_info(user, tamper(grant, lambda c: c.update(profiles=[OTHER_PROFILE]))), "grant_invalid"
    )
    refused(await gateway.profile_info(user, tamper(grant, lambda c: c.update(exp=c["exp"] + 3600))), "grant_invalid")
    # A grant is bound to its learner and lists exactly the unit's profiles.
    other_grant = await grant_for(skills, headers={"Authorization": "Bearer b"})
    refused(await gateway.profile_info(user, other_grant), "grant_invalid")
    assert (await gateway.profile_info(users["Bearer b"], other_grant)).status_code == 200
    refused(await gateway.profile_info(user, grant, OTHER_PROFILE), "profile_not_granted")
    # Signed with another key: llm-ms does not know it.
    monkeypatch.setattr(settings, "llm_grant_secret_file", None)
    monkeypatch.setattr(settings, "llm_grant_secret", "another-key-that-llm-ms-does-not-know-0123456789")
    refused(await gateway.profile_info(user, await grant_for(skills)), "grant_invalid")
    monkeypatch.setattr(settings, "llm_grant_secret", "")
    monkeypatch.setattr(settings, "llm_grant_secret_file", env_file("LLM_CONTRACT_GRANT_SECRET_FILE"))
    # Expiry means the same on both sides.
    monkeypatch.setattr(llm, "time", lambda: time() - settings.llm_grant_ttl - 60)
    refused(await gateway.profile_info(user, await grant_for(skills)), "grant_expired")


async def test_fake_mode_signs_nothing_and_completes_nothing(
    gateway: Gateway, skills: httpx.AsyncClient, users: dict[str, str]
) -> None:
    gateway.require("fake")
    graded = await gateway.grade(users["Bearer a"], await grant_for(skills), ANSWER)
    assert graded["verdict"] == "pass" and graded["receipt"] is None
    # The host sends what it got; `verdict: null` never completes.
    response = await skills.post("/rooms/graded/complete", json=completion(graded["receipt"]))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "verdict_required"
    assert (await skills.get("/rooms/graded")).json()["progress"]["status"] == "new"
    # An unsigned grading opens the host's way on without the model: `introduced`, no verdict recorded.
    fallback = completion(None) | {"answer": {"fallback": "example"}}
    response = await skills.post("/rooms/graded/complete", json=fallback)
    assert response.status_code == 200, response.text
    assert response.json()["progress"]["result"] == {"kind": "introduced"}
    async with db_context():
        assert await db.all(filter_by(models.LlmVerdict, user_id=users["Bearer a"])) == []


async def test_skills_ms_accepts_verdicts_from_llm_ms_and_refuses_tampered_ones(
    gateway: Gateway, skills: httpx.AsyncClient, users: dict[str, str], monkeypatch: MonkeyPatch
) -> None:
    gateway.require("live")
    user = users["Bearer a"]
    grant = await grant_for(skills)

    # A failing grade leaves the room open; nothing is recorded, nothing is charged here.
    failed = await gateway.grade(user, grant, FAILING_ANSWER)
    assert failed["verdict"] == "fail" and failed["score"] < failed["pass_score"]
    response = await skills.post("/rooms/graded/complete", json=completion(failed["receipt"], FAILING_ANSWER))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "verdict_failed"
    assert (await skills.get("/rooms/graded")).json()["progress"]["status"] == "new"
    forged_pass = tamper(failed["receipt"], lambda c: c.update(passed=True, score=c["max_score"]))
    assert (
        await skills.post("/rooms/graded/complete", json=completion(forged_pass, FAILING_ANSWER))
    ).status_code == 403

    passed = await gateway.grade(user, grant, ANSWER)
    assert passed["verdict"] == "pass" and passed["score"] >= passed["pass_score"]
    # Host-only details of the grade (llm-ms 28d8ee9): `flags` and per-criterion `claimed`/`rejected`.
    assert passed["flags"] == [] and all(item["met"] and item["claimed"] for item in passed["criteria"])
    receipt = passed["receipt"]
    assert isinstance(receipt, str)
    # The binding format: header `{"alg":"HS256"}`, all claims present, signed with the verdict key only.
    assert jwt.get_unverified_header(receipt) == {"alg": "HS256"}
    payload = jwt.decode(receipt, options={"verify_signature": False})
    assert set(payload) == {
        "exp", "aud", "uid", "unit_id", "course_id", "profile", "profile_hash", "request_id",
        "answer_sha256", "locale", "env", "score", "max_score", "pass_score", "passed", "model", "iat",
    }  # fmt: skip
    claims = llm.VerdictClaims.model_validate(payload)
    assert str(claims.request_id) == passed["request_id"] and claims.answer_sha256 == llm.answer_sha256(ANSWER)
    assert claims.locale == "de" and claims.uid == UUID(user) and claims.env == gateway.environment
    grant_key = env_file("LLM_CONTRACT_GRANT_SECRET_FILE").read_bytes().rstrip(b"\r\n")
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(receipt, grant_key, algorithms=["HS256"], audience="llm-verdict")

    for status, body, headers in [
        (403, completion(flip_signature(receipt)), {}),
        (403, completion(receipt, ANSWER + "!"), {}),  # another answer
        (403, completion(tamper(receipt, lambda c: c.update(unit_id="graded-too"))), {}),
        (403, completion(receipt), {"Authorization": "Bearer b"}),  # another learner
    ]:
        response = await skills.post("/rooms/graded/complete", json=body, headers=headers)
        assert response.status_code == status, response.text
    # The verdict is bound to its unit.
    assert (await skills.post("/rooms/graded-too/complete", json=completion(receipt))).status_code == 403
    # ... and to the course context of its grant.
    wrong_course = await skills.post("/rooms/graded/complete?course=prompting-course", json=completion(receipt))
    assert wrong_course.status_code == 403
    # ... and to the environment that graded it; without an own environment graded completion is off.
    monkeypatch.setattr(settings, "llm_verdict_env", "prod")
    other_env = await skills.post("/rooms/graded/complete", json=completion(receipt))
    assert (other_env.status_code, other_env.json()["code"]) == (403, "verdict_foreign"), other_env.text
    monkeypatch.setattr(settings, "llm_verdict_env", "")
    no_env = await skills.post("/rooms/graded/complete", json=completion(receipt))
    assert (no_env.status_code, no_env.json()["code"]) == (503, "verdict_unavailable"), no_env.text
    monkeypatch.setattr(settings, "llm_verdict_env", gateway.environment)

    result = await skills.post("/rooms/graded/complete", json=completion(receipt))
    assert result.status_code == 200, result.text
    assert result.json()["progress"]["status"] == "completed"
    assert result.json()["progress"]["result"] == {"kind": "introduced"}
    async with db_context():
        rows = await db.all(filter_by(models.LlmVerdict, user_id=user))
        assert [(row.request_id, row.unit_id, row.score, row.locale) for row in rows] == [
            (passed["request_id"], "graded", passed["score"], "de")
        ]

    # Single use: a repeat needs a new grading.
    start = {"request_id": str(uuid4()), "expected_revision": 1}
    assert (await skills.post("/rooms/graded/review", json=start)).status_code == 200
    review: dict[str, Any] = {"review_id": start["request_id"]}
    replay = await skills.post("/rooms/graded/complete", json=completion(receipt, revision=2, **review))
    assert (replay.status_code, replay.json()["code"]) == (409, "verdict_used"), replay.text
    # The grader language is the learner's choice and recorded with the verdict.
    again = await gateway.grade(user, grant, ANSWER, locale="en")
    repeat = await skills.post("/rooms/graded/complete", json=completion(again["receipt"], revision=2, **review))
    assert repeat.status_code == 200, repeat.text
    async with db_context():
        rows = await db.all(filter_by(models.LlmVerdict, user_id=user, request_id=again["request_id"]))
        assert [row.locale for row in rows] == ["en"]


async def test_course_context_travels_from_grant_to_verdict(
    gateway: Gateway, skills: httpx.AsyncClient, users: dict[str, str]
) -> None:
    gateway.require("live")
    grant = await grant_for(skills, params={"course": "prompting-course"})
    passed = await gateway.grade(users["Bearer a"], grant, ANSWER)
    assert (await skills.post("/rooms/graded/complete", json=completion(passed["receipt"]))).status_code == 403
    result = await skills.post("/rooms/graded/complete?course=prompting-course", json=completion(passed["receipt"]))
    assert result.status_code == 200, result.text
