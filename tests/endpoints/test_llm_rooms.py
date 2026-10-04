"""LLM lesson grants and LLM-graded completion on a disposable real SQL database, without network.

Verdicts are signed here in the binding format (llm-ms 28d8ee9: header `{"alg":"HS256"}`, payload
`{exp, ...claims}` with `locale` and `env`); the end-to-end proof against the real gateway is in `tests/contract`.
"""

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
from fastapi.exceptions import RequestValidationError
from pytest import MonkeyPatch

from api import models
from api.app import request_validation_error_response
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints.rooms import router
from api.exceptions.api_exception import CodedAPIException
from api.schemas.course import Course
from api.schemas.rooms import Catalogue
from api.schemas.user import User
from api.services import llm, rooms
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import Settings, settings


GRANT_KEY = "synthetic-grant-key-for-tests-only-0123456789"
VERDICT_KEY = "synthetic-verdict-key-for-tests-only-0123456789"
PROFILE = "llmb-grade-prompt"
PROFILE_SHA256 = sha256(b"synthetic grading profile file").hexdigest()
USER_A = "3f0c9a52-6a4e-4c1b-9d3e-5b8f2a7c1d01"
USER_B = "8a2d4e61-0b7f-4d9c-a1e3-7c6b5d4f3e02"
ANSWER = "Du bist Reiseleiter. Plane mir drei Tage in Rom. Antworte als Tabelle."
VERDICT_ENV = "test"
# The host's `LLM_FALLBACK_ANSWER`: completion without the model after comparing with the model answer.
FALLBACK = {"fallback": "example"}


def payload(revision: int = 0, **values: Any) -> dict[str, Any]:
    return {"request_id": str(uuid4()), "expected_revision": revision, **values}


def verdict_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time())
    return {
        "aud": "llm-verdict",
        "uid": USER_A,
        "unit_id": "graded",
        "course_id": None,
        "profile": PROFILE,
        "profile_hash": PROFILE_SHA256,
        "request_id": str(uuid4()),
        "answer_sha256": sha256(ANSWER.encode()).hexdigest(),
        "locale": "de",
        "env": VERDICT_ENV,
        "score": 4,
        "max_score": 4,
        "pass_score": 3,
        "passed": True,
        "model": "gpt-6-sol",
        "iat": now,
        "exp": now + 7200,
        **overrides,
    }


def sign(claims: dict[str, Any], key: str = VERDICT_KEY) -> str:
    # llm-ms' header has no `typ`; the payload carries `exp` next to the claims.
    return jwt.encode(claims, key, algorithm="HS256", headers={"typ": None})


def graded(verdict: str, revision: int = 0, answer: Any = ANSWER, **values: Any) -> dict[str, Any]:
    return payload(revision, action="complete", answer={"text": answer}, verdict=verdict, **values)


def swap_payload(token: str, change: Callable[[dict[str, Any]], None]) -> str:
    header, body, signature = token.split(".")
    claims = loads(urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    change(claims)
    encoded = urlsafe_b64encode(dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{encoded}.{signature}"


@pytest.fixture
def content(monkeypatch: MonkeyPatch) -> Catalogue:
    def unit(uid: str, **values: Any) -> dict[str, Any]:
        return {
            "id": uid,
            "path_id": "prompting",
            "title": {"de": uid, "en": uid},
            "room": "guided-lesson",
            "content": {"de": {"text": "Synthetic"}},
            "teaches": [uid],
            "practices": [],
            "requires": [],
            "retired": False,
            "completion": {"kind": "introduced", "answer": {"answer": 6}, "allow_skip": True},
            **values,
        }

    grading = {"kind": "llm-verdict", "profile": PROFILE, "profile_sha256": PROFILE_SHA256}
    value = Catalogue.model_validate(
        {
            "paths": [
                {"id": "prompting", "title": {"de": "Pfad", "en": "Path"}, "units": ["graded", "plain", "retired"]}
            ],
            "units": [
                unit("graded", llm_profiles=[PROFILE, "llmb-temperature-fan"], completion=grading),
                unit("plain"),
                unit("retired", retired=True, llm_profiles=[PROFILE]),
            ],
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(settings, "llm_grant_secret", GRANT_KEY)
    monkeypatch.setattr(settings, "llm_grant_secret_file", None)
    monkeypatch.setattr(settings, "llm_verdict_secret", VERDICT_KEY)
    monkeypatch.setattr(settings, "llm_verdict_secret_file", None)
    monkeypatch.setattr(settings, "llm_verdict_env", VERDICT_ENV)
    monkeypatch.setattr(rooms, "load_catalogue", lambda: value)
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    return value


@pytest.fixture
async def llm_client(content: Catalogue) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        users = {"Bearer a": USER_A, "Bearer b": USER_B}
        token = request.headers.get("Authorization", "")
        if token not in users:
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=users[token], email_verified=True, admin=False)

    app = FastAPI()
    app.exception_handler(RequestValidationError)(request_validation_error_response)
    app.dependency_overrides[user_auth.dependency] = identity
    # As in `api.app`: coded refusals answer `{"detail": ..., "code": ...}`.
    app.add_exception_handler(CodedAPIException, lambda _, exc: exc.response())
    app.include_router(router, dependencies=[Depends(session)])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://rooms.synthetic", headers={"Authorization": "Bearer a"}
    ) as client:
        yield client


Refusal = tuple[int, dict[str, str]]


def refusal(status: int, detail: str, code: str) -> Refusal:
    # Status and text as before the codes existed; `code` is the stable, machine-readable part.
    return status, {"detail": detail, "code": code}


def refused(response: httpx.Response) -> tuple[int, Any]:
    return response.status_code, response.json()


FOREIGN = refusal(403, "This grading does not belong to this answer", "verdict_foreign")
PRACTICE = refusal(403, "This grading comes from a test mode and does not count", "verdict_practice")
STALE = refusal(409, "This grading is out of date. Check your answer again.", "verdict_stale")
USED = refusal(409, "This grading was already used. Check your answer again.", "verdict_used")
REQUIRED = refusal(422, "Send your answer together with its grading", "verdict_required")
UNEXPECTED = refusal(422, "This room is not graded by the AI", "verdict_unexpected")
FAILED = refusal(422, "Check your answer and try again", "verdict_failed")
UNAVAILABLE = refusal(503, "The AI is not available right now", "verdict_unavailable")


def linked_course(monkeypatch: MonkeyPatch, price: int) -> Course:
    value = Course(
        id="prompting-course",
        title="Course",
        description=None,
        category=None,
        language="de",
        image=None,
        authors=[],
        price=price,
        learning_goals=[],
        requirements=[],
        last_update=0,
        learning_path_id="prompting",
    )
    monkeypatch.setattr(rooms, "COURSES", {value.id: value})
    return value


async def test_grant_carries_exactly_the_claims_llm_ms_verifies(llm_client: httpx.AsyncClient) -> None:
    before = int(time())
    response = await llm_client.post("/rooms/graded/llm-grant")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "private, no-store"
    body = response.json()
    assert set(body) == {"grant", "profiles", "expires_at"}
    assert body["profiles"] == [PROFILE, "llmb-temperature-fan"]
    assert jwt.get_unverified_header(body["grant"]) == {"alg": "HS256", "typ": "JWT"}
    claims = jwt.decode(body["grant"], GRANT_KEY, algorithms=["HS256"], audience="llm-grant")
    assert set(claims) == {"aud", "uid", "course_id", "path_id", "unit_id", "profiles", "jti", "exp"}
    assert claims["aud"] == "llm-grant" and claims["uid"] == USER_A
    assert (claims["course_id"], claims["path_id"], claims["unit_id"]) == (None, "prompting", "graded")
    assert claims["profiles"] == body["profiles"]
    assert isinstance(claims["exp"], int) and before + 7200 <= claims["exp"] <= int(time()) + 7200
    assert UUID(claims["jti"]).version == 4
    again = jwt.decode(
        (await llm_client.post("/rooms/graded/llm-grant")).json()["grant"], options={"verify_signature": False}
    )
    assert again["jti"] != claims["jti"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(body["grant"], VERDICT_KEY, algorithms=["HS256"], audience="llm-grant")
    # The catalogue binding stays server-side and a grant request writes nothing. Only the kind of the
    # completion check is public, so a host offers the completion without the model only where it applies.
    room = await llm_client.get("/rooms/graded")
    unit = room.json()["unit"]
    assert "llm_profiles" not in unit and "completion" not in unit and unit["completion_kind"] == "llm-verdict"
    assert PROFILE_SHA256 not in room.text and "profile_sha256" not in room.text
    assert (await llm_client.get("/rooms/plain")).json()["unit"]["completion_kind"] == "introduced"
    async with db_context():
        for model in (models.RoomState, models.RoomRequest, models.PurchaseUser, models.LlmVerdict):
            assert await db.all(filter_by(model, user_id=USER_A)) == []


async def test_grant_needs_an_llm_unit_access_and_a_key(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    assert (await llm_client.post("/rooms/plain/llm-grant")).status_code == 404
    assert (await llm_client.post("/rooms/retired/llm-grant")).status_code == 404
    assert (await llm_client.post("/rooms/unknown/llm-grant")).status_code == 404
    assert (await llm_client.post("/rooms/graded/llm-grant", headers={"Authorization": ""})).status_code == 401
    monkeypatch.setattr(settings, "llm_grant_secret", "")
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 503
    monkeypatch.setattr(settings, "llm_grant_secret", GRANT_KEY)
    monkeypatch.setattr(settings, "rooms_enabled", False)
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 404


async def test_grant_follows_the_paid_course_admission(llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch) -> None:
    course = linked_course(monkeypatch, price=100)
    premium = AsyncMock(return_value=False)
    monkeypatch.setattr(rooms, "has_premium", premium)
    monkeypatch.setattr(rooms, "get_owned_courses", AsyncMock(return_value=set()))
    assert (await llm_client.post(f"/rooms/graded/llm-grant?course={course.id}")).status_code == 403
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 403
    assert (await llm_client.post("/rooms/graded/llm-grant?course=unknown")).status_code == 404
    premium.return_value = True
    response = await llm_client.post(f"/rooms/graded/llm-grant?course={course.id}")
    assert response.status_code == 200
    claims = jwt.decode(response.json()["grant"], GRANT_KEY, algorithms=["HS256"], audience="llm-grant")
    assert claims["course_id"] == course.id and claims["unit_id"] == "graded"


async def test_passing_verdict_completes_once_without_xp_or_answer_text(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    claims = verdict_claims()
    body = graded(sign(claims))
    result = await llm_client.post("/rooms/graded/complete", json=body)
    assert result.status_code == 200
    assert result.json()["progress"]["status"] == "completed"
    assert result.json()["progress"]["result"] == {"kind": "introduced"}
    # An exact retry is answered from the receipt; the verdict is recorded once.
    assert (await llm_client.post("/rooms/graded/complete", json=body)).json() == result.json()
    async with db_context():
        rows = await db.all(filter_by(models.LlmVerdict, user_id=USER_A))
        assert len(rows) == 1
        row = rows[0]
        assert (row.request_id, row.unit_id, row.profile) == (claims["request_id"], "graded", PROFILE)
        assert (row.profile_sha256, row.answer_sha256) == (PROFILE_SHA256, claims["answer_sha256"])
        assert (row.score, row.max_score, row.model, row.locale) == (4, 4, "gpt-6-sol", "de")
        assert int(row.graded_at.timestamp()) == claims["iat"]
        for model in (models.XP, models.XPOperation, models.CourseAccess, models.LectureProgress):
            assert await db.all(filter_by(model, user_id=USER_A)) == []
        export = await export_user_data(USER_A)
        assert [item["request_id"] for item in export.llm_verdicts] == [claims["request_id"]]
        assert ANSWER not in export.model_dump_json()
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await delete_user_data(USER_A)
    async with db_context():
        assert await db.all(filter_by(models.LlmVerdict, user_id=USER_A)) == []


async def test_failing_verdict_keeps_the_room_open_and_a_new_grading_can_pass(llm_client: httpx.AsyncClient) -> None:
    saved = await llm_client.put("/rooms/graded/state", json=payload(state={"draft": ANSWER}))
    assert saved.status_code == 200
    failing = graded(sign(verdict_claims(score=1, passed=False)), 1)
    assert refused(await llm_client.post("/rooms/graded/complete", json=failing)) == FAILED
    room = (await llm_client.get("/rooms/graded")).json()
    assert room["progress"] == saved.json()["progress"]
    async with db_context():
        assert await db.all(filter_by(models.LlmVerdict, user_id=USER_A)) == []
        assert await db.get(models.RoomRequest, user_id=USER_A, request_id=failing["request_id"]) is None
    passing = await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 1))
    assert passing.status_code == 200 and passing.json()["progress"]["status"] == "completed"


def _other_answer(claims: dict[str, Any]) -> None:
    claims["answer_sha256"] = sha256(b"another answer").hexdigest()


def _passed(claims: dict[str, Any]) -> None:
    claims["passed"] = True


def _without(name: str) -> Callable[[], str]:
    return lambda: sign({key: value for key, value in verdict_claims().items() if key != name})


@pytest.mark.parametrize(
    "token,expected",
    [
        (lambda: sign(verdict_claims(), GRANT_KEY), FOREIGN),  # the grant key never signs a pass
        (lambda: sign(verdict_claims(), "some-other-key-0123456789-0123456789"), FOREIGN),
        (lambda: sign(verdict_claims(fake=True)), PRACTICE),  # practice verdicts never complete
        (lambda: sign(verdict_claims(test=True)), PRACTICE),
        (lambda: sign(verdict_claims(mode="fake")), PRACTICE),
        (lambda: sign(verdict_claims(provider="fake")), PRACTICE),
        (lambda: sign(verdict_claims(locale="fr")), FOREIGN),
        (_without("locale"), FOREIGN),
        (_without("profile_hash"), FOREIGN),
        # A verdict counts only in the environment that graded it.
        (_without("env"), FOREIGN),
        (lambda: sign(verdict_claims(env="prod")), FOREIGN),
        (lambda: sign(verdict_claims(env="Test")), FOREIGN),
        (lambda: sign(verdict_claims(env="")), FOREIGN),
        (lambda: sign(verdict_claims(env=None)), FOREIGN),
        (lambda: sign(verdict_claims(uid=USER_B)), FOREIGN),
        (lambda: sign(verdict_claims(unit_id="plain")), FOREIGN),
        (lambda: sign(verdict_claims(course_id="prompting-course")), FOREIGN),
        (lambda: sign(verdict_claims(profile="llmb-temperature-fan")), FOREIGN),
        (lambda: sign(verdict_claims(answer_sha256=sha256(b"another answer").hexdigest())), FOREIGN),
        (lambda: sign(verdict_claims(aud="llm-grant")), FOREIGN),  # a lesson grant is no verdict
        (lambda: sign(verdict_claims(score="4")), FOREIGN),
        (lambda: sign(verdict_claims(passed=1)), FOREIGN),
        (lambda: sign(verdict_claims(iat=int(time()) + 600)), FOREIGN),
        (lambda: swap_payload(sign(verdict_claims()), _other_answer), FOREIGN),
        (lambda: swap_payload(sign(verdict_claims(passed=False, score=0)), _passed), FOREIGN),
        (lambda: jwt.encode(verdict_claims(), VERDICT_KEY, algorithm="HS512"), FOREIGN),
        (lambda: sign(verdict_claims(exp=int(time()) - 1)), STALE),  # expired: grade again
        (lambda: sign(verdict_claims(profile_hash="0" * 64)), STALE),  # other rubric version: grade again
    ],
)
async def test_forged_foreign_or_stale_verdicts_do_not_complete(
    llm_client: httpx.AsyncClient, token: Callable[[], str], expected: Refusal
) -> None:
    assert refused(await llm_client.post("/rooms/graded/complete", json=graded(token()))) == expected
    assert (await llm_client.get("/rooms/graded")).json()["progress"]["status"] == "new"
    async with db_context():
        assert await db.all(filter_by(models.LlmVerdict, user_id=USER_A)) == []


async def test_verdict_belongs_to_its_user_and_answer(llm_client: httpx.AsyncClient) -> None:
    token = sign(verdict_claims())
    other = await llm_client.post("/rooms/graded/complete", json=graded(token), headers={"Authorization": "Bearer b"})
    assert refused(other) == FOREIGN
    changed = await llm_client.post("/rooms/graded/complete", json=graded(token, answer=ANSWER + " "))
    assert refused(changed) == FOREIGN
    assert (await llm_client.post("/rooms/graded/complete", json=graded(token))).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"action": "complete", "answer": {"text": ANSWER}},
        {"action": "complete", "answer": {"text": ANSWER, "passed": True}, "verdict": "a.b.c"},
        {"action": "complete", "answer": {"text": 7}, "verdict": "a.b.c"},
        {"action": "complete", "answer": {}, "verdict": "a.b.c"},
        {"action": "complete", "answer": {"text": ANSWER}, "verdict": "not a token"},
        {"action": "complete", "answer": {"text": ANSWER}, "verdict": None},  # llm-ms `receipt: null`
    ],
)
async def test_graded_completion_needs_answer_text_and_verdict(
    llm_client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    assert (await llm_client.post("/rooms/graded/complete", json=payload(**body))).status_code == 422


async def test_verdict_is_refused_where_no_grading_applies_and_skip_stays_open(llm_client: httpx.AsyncClient) -> None:
    token = sign(verdict_claims(unit_id="plain"))
    body = payload(action="complete", answer={"answer": 6}, verdict=token)
    assert refused(await llm_client.post("/rooms/plain/complete", json=body)) == UNEXPECTED
    skipped = await llm_client.post("/rooms/graded/complete", json=payload(action="skip"))
    assert skipped.status_code == 200 and skipped.json()["progress"]["status"] == "skipped"


async def test_verdict_is_single_use_and_a_repeat_needs_a_new_grading(llm_client: httpx.AsyncClient) -> None:
    first = sign(verdict_claims())
    assert (await llm_client.post("/rooms/graded/complete", json=graded(first))).status_code == 200
    start = payload(1)
    assert (await llm_client.post("/rooms/graded/review", json=start)).status_code == 200
    review = {"review_id": start["request_id"]}
    assert refused(await llm_client.post("/rooms/graded/complete", json=graded(first, 2, **review))) == USED
    older = sign(verdict_claims(iat=int(time()) - 60))
    assert refused(await llm_client.post("/rooms/graded/complete", json=graded(older, 2, **review))) == STALE
    fresh = await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 2, **review))
    assert fresh.status_code == 200 and fresh.json()["progress"]["status"] == "completed"
    async with db_context():
        assert len(await db.all(filter_by(models.LlmVerdict, user_id=USER_A))) == 2
        row = await db.get(models.RoomState, user_id=USER_A, unit_id="graded")
        assert row is not None and row.status == "completed" and row.review_status == "completed"


async def test_verdict_key_never_falls_back_to_the_grant_key(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    by_grant_key = graded(sign(verdict_claims(), GRANT_KEY))
    assert (await llm_client.post("/rooms/graded/complete", json=by_grant_key)).status_code == 403
    monkeypatch.setattr(settings, "llm_verdict_secret", "")
    assert llm.verdict_key() is None
    assert refused(await llm_client.post("/rooms/graded/complete", json=by_grant_key)) == UNAVAILABLE
    monkeypatch.setattr(settings, "llm_verdict_secret", GRANT_KEY)  # the same bytes as the grant key: refused
    assert llm.verdict_key() is None
    assert (await llm_client.post("/rooms/graded/complete", json=by_grant_key)).status_code == 503
    monkeypatch.setattr(settings, "llm_verdict_secret", VERDICT_KEY)
    assert (await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))).status_code == 200


async def test_keys_from_credential_files_match_llm_ms_and_fail_closed(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    grant_file, verdict_file = tmp_path / "grant-secret", tmp_path / "verdict-secret"
    grant_file.write_bytes(GRANT_KEY.encode() + b"\r\n\n")
    verdict_file.write_bytes(VERDICT_KEY.encode() + b"\n")
    monkeypatch.setattr(settings, "llm_grant_secret", "")
    monkeypatch.setattr(settings, "llm_grant_secret_file", grant_file)
    monkeypatch.setattr(settings, "llm_verdict_secret", "")
    monkeypatch.setattr(settings, "llm_verdict_secret_file", verdict_file)
    # llm-ms reads credential files with trailing CR/LF removed and nothing else.
    assert (llm.grant_key(), llm.verdict_key()) == (GRANT_KEY.encode(), VERDICT_KEY.encode())
    grant = (await llm_client.post("/rooms/graded/llm-grant")).json()["grant"]
    assert jwt.decode(grant, GRANT_KEY, algorithms=["HS256"], audience="llm-grant")["unit_id"] == "graded"
    assert (await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))).status_code == 200
    verdict_file.write_bytes(GRANT_KEY.encode() + b"\n")  # a copy of the grant key file
    assert llm.verdict_key() is None
    grant_file.write_bytes(b" " + GRANT_KEY.encode() + b"\n")
    assert llm.grant_key() == b" " + GRANT_KEY.encode()
    monkeypatch.setattr(settings, "llm_grant_secret", GRANT_KEY)  # value and file: ambiguous, so refused
    assert llm.grant_key() is None
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 503
    monkeypatch.setattr(settings, "llm_verdict_secret", VERDICT_KEY)
    assert llm.verdict_key() is None
    monkeypatch.setattr(settings, "llm_grant_secret", "")
    monkeypatch.setattr(settings, "llm_grant_secret_file", tmp_path / "missing")
    assert llm.grant_key() is None
    monkeypatch.setenv("LLM_GRANT_SECRET_FILE", "")
    assert Settings().llm_grant_secret_file is None


@pytest.mark.parametrize("locale", ["de", "en"])
async def test_grader_language_is_recorded_and_unknown_claims_are_ignored(
    llm_client: httpx.AsyncClient, locale: str
) -> None:
    # Units are bilingual and the client chooses the grader language, so either one counts and is kept.
    token = sign(verdict_claims(locale=locale, rubric_note="a later extension", version=2))
    assert (await llm_client.post("/rooms/graded/complete", json=graded(token))).status_code == 200
    async with db_context():
        rows = await db.all(filter_by(models.LlmVerdict, user_id=USER_A))
        assert [row.locale for row in rows] == [locale]


@pytest.mark.parametrize("name", ["jwt_secret", "internal_jwt_secret_auth", "internal_jwt_secret_skills"])
async def test_llm_keys_are_long_and_differ_from_every_other_key(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, name: str
) -> None:
    assert (llm.grant_key(), llm.verdict_key()) == (GRANT_KEY.encode(), VERDICT_KEY.encode())
    monkeypatch.setattr(settings, "llm_verdict_secret", "v" * 31)
    assert llm.verdict_key() is None
    monkeypatch.setattr(settings, "llm_verdict_secret", "v" * 32)
    assert llm.verdict_key() == b"v" * 32
    monkeypatch.setattr(settings, "llm_grant_secret", "g" * 31)
    assert llm.grant_key() is None
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 503
    monkeypatch.setattr(settings, "llm_grant_secret", GRANT_KEY)
    monkeypatch.setattr(settings, "llm_verdict_secret", VERDICT_KEY)
    monkeypatch.setattr(settings, name, VERDICT_KEY)
    assert llm.verdict_key() is None
    assert (await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))).status_code == 503
    monkeypatch.setattr(settings, name, GRANT_KEY)
    assert llm.grant_key() is None
    assert (await llm_client.post("/rooms/graded/llm-grant")).status_code == 503


async def test_a_verdict_counts_only_in_the_environment_that_graded_it(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    token = sign(verdict_claims(env="test"))
    monkeypatch.setattr(settings, "llm_verdict_env", "prod")
    assert refused(await llm_client.post("/rooms/graded/complete", json=graded(token))) == FOREIGN
    monkeypatch.setattr(settings, "llm_verdict_env", "test")
    assert (await llm_client.post("/rooms/graded/complete", json=graded(token))).status_code == 200


@pytest.mark.parametrize("configured", ["", "Prod", "prod!", "-test", "p" * 33])
async def test_graded_completion_is_off_without_a_valid_own_environment_but_the_fallback_works(
    llm_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, configured: str
) -> None:
    monkeypatch.setattr(settings, "llm_verdict_env", configured)
    assert llm.verdict_env() is None
    assert refused(await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))) == UNAVAILABLE
    # The completion without the model needs no verdict check, so it stays open when the AI is off.
    fallback = await llm_client.post("/rooms/graded/complete", json=payload(action="complete", answer=FALLBACK))
    assert fallback.status_code == 200 and fallback.json()["progress"]["status"] == "completed"
    monkeypatch.setattr(settings, "llm_verdict_env", "p" * 32)
    assert llm.verdict_env() == "p" * 32


async def test_fallback_without_the_model_completes_as_introduced_without_a_verdict(
    llm_client: httpx.AsyncClient, content: Catalogue
) -> None:
    saved = await llm_client.put("/rooms/graded/state", json=payload(state={"draft": ANSWER}))
    assert saved.status_code == 200
    body = payload(1, action="complete", answer=FALLBACK)
    completed = await llm_client.post("/rooms/graded/complete", json=body)
    assert completed.status_code == 200
    assert completed.json()["progress"]["status"] == "completed"
    assert completed.json()["progress"]["result"] == {"kind": "introduced"}
    # A lost response replayed with the same request is answered from the receipt.
    assert (await llm_client.post("/rooms/graded/complete", json=body)).json() == completed.json()
    # Another completion of the finished room is a room conflict, with the plain error body as before.
    again = await llm_client.post("/rooms/graded/complete", json=payload(2, action="complete", answer=FALLBACK))
    assert refused(again) == (409, {"detail": "This room has already been finished"})
    async with db_context():
        # No verdict row: that is what tells the fallback from a graded pass. No XP or milestone either.
        assert await db.all(filter_by(models.LlmVerdict, user_id=USER_A)) == []
        for model in (models.XP, models.XPOperation, models.LessonMilestoneDelivery):
            assert await db.all(filter_by(model, user_id=USER_A)) == []
        row = await db.get(models.RoomState, user_id=USER_A, unit_id="graded")
        assert row is not None and (row.status, row.result, row.revision) == ("completed", {"kind": "introduced"}, 2)
        assert len(await db.all(filter_by(models.RoomRequest, user_id=USER_A, unit_id="graded"))) == 2
        # Its concepts count as introduced, as after a skip.
        assert rooms.introduced_concepts(content, await rooms.read_states(USER_A)) == {"graded"}


@pytest.mark.parametrize(
    "values",
    [
        {"answer": {"text": ANSWER}},  # an answer without its grading
        {"answer": {"text": ANSWER}, "verdict": None},  # llm-ms `receipt: null`
        {"answer": {**FALLBACK, "text": ANSWER}},
        {"answer": {"fallback": "Example"}},
        {"answer": {"fallback": True}},
        {"answer": {"fallback": ["example"]}},
        {"answer": {"fallback": {"example": True}}},
        {"answer": {}},
        {},
        # The fallback never carries a verdict, not even a valid passing one.
        {"answer": FALLBACK, "verdict": sign(verdict_claims())},
    ],
)
async def test_without_a_verdict_only_the_exact_fallback_answer_completes(
    llm_client: httpx.AsyncClient, values: dict[str, Any]
) -> None:
    response = await llm_client.post("/rooms/graded/complete", json=payload(action="complete", **values))
    assert refused(response) == REQUIRED
    assert (await llm_client.get("/rooms/graded")).json()["progress"]["status"] == "new"
    async with db_context():
        assert await db.all(filter_by(models.RoomRequest, user_id=USER_A)) == []
        assert await db.all(filter_by(models.LlmVerdict, user_id=USER_A)) == []


async def test_the_fallback_answer_is_a_wrong_answer_where_no_grading_applies(llm_client: httpx.AsyncClient) -> None:
    response = await llm_client.post("/rooms/plain/complete", json=payload(action="complete", answer=FALLBACK))
    assert refused(response) == (422, {"detail": "Check your answer and try again"})
    assert (await llm_client.get("/rooms/plain")).json()["progress"]["status"] == "new"


async def test_a_graded_pass_after_the_fallback_follows_the_repeat_rules(llm_client: httpx.AsyncClient) -> None:
    fallback = await llm_client.post("/rooms/graded/complete", json=payload(action="complete", answer=FALLBACK))
    assert fallback.status_code == 200
    # The first round is over: a later pass does not turn the fallback into a pass, and its verdict stays unused.
    late = await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 1))
    assert refused(late) == (409, {"detail": "This room has already been finished"})
    # A repeat round takes a grading made during it, exactly as after a graded pass. The repeat completes
    # and records its verdict; the first result stays `introduced`.
    start = payload(1)
    assert (await llm_client.post("/rooms/graded/review", json=start)).status_code == 200
    review = {"review_id": start["request_id"]}
    passed = await llm_client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 2, **review))
    assert passed.status_code == 200 and passed.json()["progress"]["status"] == "completed"
    # The fallback completes a repeat round as well.
    start = payload(3)
    assert (await llm_client.post("/rooms/graded/review", json=start)).status_code == 200
    body = payload(4, action="complete", answer=FALLBACK, review_id=start["request_id"])
    repeated = await llm_client.post("/rooms/graded/complete", json=body)
    assert repeated.status_code == 200 and repeated.json()["progress"]["status"] == "completed"
    async with db_context():
        row = await db.get(models.RoomState, user_id=USER_A, unit_id="graded")
        assert row is not None
        assert (row.status, row.result, row.review_status) == ("completed", {"kind": "introduced"}, "completed")
        assert len(await db.all(filter_by(models.LlmVerdict, user_id=USER_A))) == 1
