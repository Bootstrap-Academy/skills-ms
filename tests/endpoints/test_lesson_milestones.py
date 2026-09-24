"""Lesson milestones (XP-02): a checked completion books XP in challenges-ms, once per learner and eventually.

Runs on a disposable real SQL database; challenges-ms is a synthetic HTTP double with the response shapes of
challenges-ms `d981e3f` (`PUT /_internal/lesson-milestones/{user_id}/{unit_id}`).
"""

from datetime import timedelta
from json import loads
from time import time
from typing import Any, AsyncIterator, Callable
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError
from pytest import MonkeyPatch
from sqlalchemy import update

from api import models
from api.auth import user_auth
from api.database import db, db_context, filter_by
from api.endpoints.rooms import router
from api.schemas.rooms import Catalogue, CatalogueUnit, Complete
from api.schemas.user import User
from api.services import lesson_milestones, llm, rooms
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.settings import settings
from api.utils.utc import utcnow
from tests.endpoints.test_llm_rooms import (
    ANSWER,
    FALLBACK,
    PROFILE,
    PROFILE_SHA256,
    USER_A,
    VERDICT_ENV,
    VERDICT_KEY,
    graded,
    payload,
    sign,
    verdict_claims,
)


CHALLENGES = "http://challenges.synthetic/base"
INTERNAL_KEY = "synthetic-internal-challenges-key-for-tests-0123456789"
SKILL = "prompting_basics"
# The test client itself must not go through the challenges-ms double.
REAL_CLIENT = httpx.AsyncClient


class Challenges:
    """Records every call; `reply` decides the answer (default: the first booking wins, like challenges-ms)."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.booked: set[tuple[str, str]] = set()
        self.reply: Callable[[httpx.Request], httpx.Response] = self.book

    def book(self, request: httpx.Request) -> httpx.Response:
        *_, user_id, unit_id = request.url.path.split("/")
        created = (user_id, unit_id) not in self.booked
        self.booked.add((user_id, unit_id))
        milestone = {"id": str(uuid4()), "user_id": user_id, "unit_id": unit_id, **loads(request.content)}
        return httpx.Response(200, json={"created": created, "milestone": milestone})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.reply(request)


def down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("challenges-ms is down", request=request)


@pytest.fixture
def challenges(monkeypatch: MonkeyPatch) -> Challenges:
    fake = Challenges()
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(fake.handle))
    )
    monkeypatch.setattr(settings, "challenges_url", CHALLENGES)
    monkeypatch.setattr(settings, "internal_jwt_secret_challenges", INTERNAL_KEY)
    return fake


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
    value = Catalogue.parse_obj(
        {
            "paths": [
                {"id": "prompting", "title": {"de": "Pfad", "en": "Path"}, "units": ["graded", "checked", "plain"]}
            ],
            "units": [
                unit("graded", llm_profiles=[PROFILE], completion=grading, milestone={"skill_id": SKILL, "xp": 20}),
                unit("checked", milestone={"skill_id": SKILL, "xp": 10}),
                unit("plain"),
            ],
        }
    )
    monkeypatch.setattr(settings, "rooms_enabled", True)
    monkeypatch.setattr(settings, "learning_rooms_exercise_refs", {})
    monkeypatch.setattr(settings, "llm_verdict_secret", VERDICT_KEY)
    monkeypatch.setattr(settings, "llm_verdict_secret_file", None)
    monkeypatch.setattr(settings, "llm_verdict_env", VERDICT_ENV)
    monkeypatch.setattr(rooms, "load_catalogue", lambda: value)
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    return value


@pytest.fixture
async def client(content: Catalogue, challenges: Challenges) -> AsyncIterator[httpx.AsyncClient]:
    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        if request.headers.get("Authorization") != "Bearer a":
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=USER_A, email_verified=True, admin=False)

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = identity
    app.include_router(router, dependencies=[Depends(session)])
    async with REAL_CLIENT(app=app, base_url="http://rooms.synthetic", headers={"Authorization": "Bearer a"}) as value:
        yield value
    # Deliveries started by a completion finish before the database goes away.
    await lesson_milestones.settle()


async def outbox() -> list[models.LessonMilestoneDelivery]:
    async with db_context():
        return list(await db.all(filter_by(models.LessonMilestoneDelivery, user_id=USER_A)))


async def make_due() -> None:
    async with db_context():
        await db.exec(update(models.LessonMilestoneDelivery).values(next_attempt_at=utcnow() - timedelta(seconds=1)))


async def test_verified_pass_books_the_milestone_once_with_the_exact_payload(
    client: httpx.AsyncClient, challenges: Challenges
) -> None:
    before = int(time())
    body = graded(sign(verdict_claims()))
    completed = await client.post("/rooms/graded/complete", json=body)
    assert completed.status_code == 200 and completed.json()["progress"]["status"] == "completed"
    await lesson_milestones.settle()

    assert len(challenges.calls) == 1
    call = challenges.calls[0]
    assert call.method == "PUT"
    assert str(call.url) == f"{CHALLENGES}/_internal/lesson-milestones/{USER_A}/graded"
    assert loads(call.content) == {"skill_id": SKILL, "xp": 20, "completion": "llm_verdict"}
    scheme, token = call.headers["Authorization"].split(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(token, INTERNAL_KEY, algorithms=["HS256"], audience="challenges")
    assert set(claims) == {"aud", "exp"} and before < claims["exp"] <= int(time()) + settings.internal_jwt_ttl
    [row] = await outbox()
    assert (row.unit_id, row.state, row.attempts, row.last_status) == ("graded", "delivered", 1, 200)
    assert row.finished_at is not None

    # A lost response replayed, a second completion, a repeat round and the recovery loop book nothing more.
    assert (await client.post("/rooms/graded/complete", json=body)).json() == completed.json()
    again = await client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 1))
    assert again.status_code == 409
    review = await client.post("/rooms/graded/review", json=payload(1))
    assert review.status_code == 200
    review_id = review.json()["progress"]["review_id"]
    repeated = await client.post("/rooms/graded/complete", json=graded(sign(verdict_claims()), 2, review_id=review_id))
    assert repeated.status_code == 200 and repeated.json()["progress"]["status"] == "completed"
    await lesson_milestones.recover()
    await lesson_milestones.settle()
    assert len(challenges.calls) == 1
    assert len(await outbox()) == 1


async def test_deterministic_check_books_its_own_milestone(client: httpx.AsyncClient, challenges: Challenges) -> None:
    wrong = await client.post("/rooms/checked/complete", json=payload(action="complete", answer={"answer": 5}))
    assert wrong.status_code == 422
    right = await client.post("/rooms/checked/complete", json=payload(action="complete", answer={"answer": 6}))
    assert right.status_code == 200
    await lesson_milestones.settle()
    assert [loads(call.content) for call in challenges.calls] == [
        {"skill_id": SKILL, "xp": 10, "completion": "deterministic"}
    ]
    assert [(row.unit_id, row.state) for row in await outbox()] == [("checked", "delivered")]


async def test_skips_failures_and_units_without_milestone_book_nothing(
    client: httpx.AsyncClient, challenges: Challenges
) -> None:
    failing = await client.post("/rooms/graded/complete", json=graded(sign(verdict_claims(score=1, passed=False))))
    assert failing.status_code == 422
    assert (await client.post("/rooms/graded/complete", json=payload(action="skip"))).status_code == 200
    assert (await client.post("/rooms/checked/complete", json=payload(action="skip"))).status_code == 200
    plain = await client.post("/rooms/plain/complete", json=payload(action="complete", answer={"answer": 6}))
    assert plain.status_code == 200 and plain.json()["progress"]["status"] == "completed"
    # Completing the review of a skipped lesson introduces it, but an exact answer check books only in the first
    # round; only a verified LLM pass counts in a repeat (see the round tests below).
    review = await client.post("/rooms/checked/review", json=payload(1))
    review_id = review.json()["progress"]["review_id"]
    finished = await client.post(
        "/rooms/checked/complete", json=payload(2, action="complete", answer={"answer": 6}, review_id=review_id)
    )
    assert finished.status_code == 200 and finished.json()["progress"]["status"] == "completed"
    await lesson_milestones.recover()
    await lesson_milestones.settle()
    assert challenges.calls == [] and await outbox() == []


async def test_a_completion_without_a_verified_verdict_never_books(
    client: httpx.AsyncClient, challenges: Challenges, content: Catalogue
) -> None:
    unit = next(unit for unit in content.units if unit.id == "graded")
    passed = llm.VerdictClaims.parse_obj({**verdict_claims(), "request_id": str(uuid4())})
    complete = Complete.parse_obj(payload(action="complete", answer={"text": ANSWER}))
    skip = Complete.parse_obj(payload(action="skip"))
    fallback = Complete.parse_obj(payload(action="complete", answer=FALLBACK))
    assert rooms.fallback_completion(unit, fallback)
    for repeat in (False, True):
        assert lesson_milestones.checked_completion(unit, complete, passed, repeat=repeat) == "llm_verdict"
        assert lesson_milestones.checked_completion(unit, complete, None, repeat=repeat) is None
        failed = passed.copy(update={"passed": False})
        assert lesson_milestones.checked_completion(unit, complete, failed, repeat=repeat) is None
        assert lesson_milestones.checked_completion(unit, skip, passed, repeat=repeat) is None
        assert lesson_milestones.checked_completion(unit, fallback, None, repeat=repeat) is None
    # An exact answer check counts only in the first round.
    checked = next(unit for unit in content.units if unit.id == "checked")
    right = Complete.parse_obj(payload(action="complete", answer={"answer": 6}))
    assert lesson_milestones.checked_completion(checked, right, None, repeat=False) == "deterministic"
    assert lesson_milestones.checked_completion(checked, right, None, repeat=True) is None

    # An answer without its grading completes nothing.
    response = await client.post("/rooms/graded/complete", json=payload(action="complete", answer={"text": ANSWER}))
    assert response.status_code == 422
    # The fallback without the model completes the milestone unit as `introduced`, and books nothing,
    # also when the same request is replayed.
    body = payload(action="complete", answer=FALLBACK)
    response = await client.post("/rooms/graded/complete", json=body)
    assert response.status_code == 200
    progress = response.json()["progress"]
    assert (progress["status"], progress["result"]) == ("completed", {"kind": "introduced"})
    assert (await client.post("/rooms/graded/complete", json=body)).json() == response.json()
    # The fallback in a repeat round books nothing either (a later verified pass would, see the round tests).
    review = await client.post("/rooms/graded/review", json=payload(1))
    assert review.status_code == 200
    review_id = review.json()["progress"]["review_id"]
    repeated = await client.post(
        "/rooms/graded/complete", json=payload(2, action="complete", answer=FALLBACK, review_id=review_id)
    )
    assert repeated.status_code == 200 and repeated.json()["progress"]["status"] == "completed"
    # At the deterministic unit the fallback answer is simply wrong.
    wrong = await client.post("/rooms/checked/complete", json=payload(action="complete", answer=FALLBACK))
    assert wrong.status_code == 422
    await lesson_milestones.recover()
    await lesson_milestones.settle()
    assert challenges.calls == [] and await outbox() == []
    async with db_context():
        for model in (models.XP, models.XPOperation):
            assert await db.all(filter_by(model, user_id=USER_A)) == []


async def finish_round(client: httpx.AsyncClient, number: int, how: str) -> None:
    """Round 0 is the first run through the graded unit; every later round is a repeat started for it."""
    values: dict[str, Any] = {}
    revision = 0
    if number > 0:
        review = await client.post("/rooms/graded/review", json=payload(2 * number - 1))
        assert review.status_code == 200
        values["review_id"] = review.json()["progress"]["review_id"]
        revision = 2 * number
    if how == "graded":
        body = graded(sign(verdict_claims()), revision, **values)
    elif how == "fallback":
        body = payload(revision, action="complete", answer=FALLBACK, **values)
    else:
        body = payload(revision, action="skip", **values)
    response = await client.post("/rooms/graded/complete", json=body)
    assert response.status_code == 200
    assert response.json()["progress"]["status"] == ("skipped" if how == "skip" else "completed")


@pytest.mark.parametrize(
    "rounds,booked",
    [
        # PO 24.09.: the first verified pass books the milestone if it was never booked, also in a repeat.
        (["fallback", "graded"], True),
        (["fallback", "fallback", "graded"], True),
        # A skip never books, so it does not use the milestone up either: a later verified pass books it.
        (["skip", "graded"], True),
        # Once booked, a second verified pass books nothing more.
        (["graded", "graded"], True),
        (["fallback", "graded", "graded"], True),
        (["skip", "graded", "fallback", "graded"], True),
        # Without a verified pass nothing is ever booked.
        (["fallback", "fallback"], False),
        (["skip", "skip"], False),
        (["skip", "fallback"], False),
    ],
)
async def test_the_first_verified_pass_books_the_milestone_once_also_in_a_repeat(
    client: httpx.AsyncClient, challenges: Challenges, rounds: list[str], booked: bool
) -> None:
    for number, how in enumerate(rounds):
        await finish_round(client, number, how)
        await lesson_milestones.settle()
    await lesson_milestones.recover()
    await lesson_milestones.settle()
    if not booked:
        assert challenges.calls == [] and await outbox() == []
        return
    # Exactly one delivery, booked in challenges-ms as new, right after the pass that earned it.
    assert [loads(call.content) for call in challenges.calls] == [
        {"skill_id": SKILL, "xp": 20, "completion": "llm_verdict"}
    ]
    assert str(challenges.calls[0].url) == f"{CHALLENGES}/_internal/lesson-milestones/{USER_A}/graded"
    assert challenges.booked == {(USER_A, "graded")}
    [row] = await outbox()
    assert (row.unit_id, row.completion, row.state, row.attempts) == ("graded", "llm_verdict", "delivered", 1)
    # Every verified pass is still recorded as a used verdict; only the first one booked.
    async with db_context():
        verdicts = await db.all(filter_by(models.LlmVerdict, user_id=USER_A))
    assert len(verdicts) == rounds.count("graded")


async def test_a_pass_in_a_repeat_while_the_first_booking_is_pending_books_nothing_more(
    client: httpx.AsyncClient, challenges: Challenges
) -> None:
    challenges.reply = down
    await finish_round(client, 0, "graded")
    await lesson_milestones.settle()
    await finish_round(client, 1, "graded")
    await lesson_milestones.settle()
    [row] = await outbox()
    assert (row.state, row.attempts) == ("pending", 1)
    challenges.reply = challenges.book
    await make_due()
    await lesson_milestones.recover()
    [row] = await outbox()
    assert (row.state, row.attempts) == ("delivered", 2)
    assert len(challenges.calls) == 2 and challenges.booked == {(USER_A, "graded")}


async def test_challenges_down_keeps_the_completion_and_delivers_later(
    client: httpx.AsyncClient, challenges: Challenges
) -> None:
    challenges.reply = down
    completed = await client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))
    assert completed.status_code == 200 and completed.json()["progress"]["status"] == "completed"
    await lesson_milestones.settle()
    assert len(challenges.calls) == 1
    [row] = await outbox()
    assert (row.state, row.attempts, row.last_status) == ("pending", 1, None)
    assert timedelta(seconds=25) < row.next_attempt_at - utcnow() <= timedelta(seconds=30)
    room = await client.get("/rooms/graded")
    assert room.json()["progress"]["status"] == "completed"

    # Not due yet: the loop leaves it alone.
    await lesson_milestones.recover()
    assert len(challenges.calls) == 1

    # Still down when due: the next wait doubles.
    await make_due()
    await lesson_milestones.recover()
    [row] = await outbox()
    assert (row.state, row.attempts) == ("pending", 2)
    assert timedelta(seconds=55) < row.next_attempt_at - utcnow() <= timedelta(seconds=60)

    # Back up: the same payload is delivered once, and a later sweep sends nothing.
    challenges.reply = challenges.book
    await make_due()
    await lesson_milestones.recover()
    await lesson_milestones.recover()
    assert len(challenges.calls) == 3
    assert {call.content for call in challenges.calls} == {challenges.calls[0].content}
    assert loads(challenges.calls[-1].content) == {"skill_id": SKILL, "xp": 20, "completion": "llm_verdict"}
    [row] = await outbox()
    assert (row.state, row.attempts, row.last_status) == ("delivered", 3, 200)


@pytest.mark.parametrize(
    "reply,outcome",
    [
        (httpx.Response(200, json={"created": False, "milestone": {"unit_id": "graded"}}), "delivered"),
        (httpx.Response(200, json={"created": True, "milestone": {"unit_id": "other"}}), "retry"),
        (httpx.Response(200, text="not json"), "retry"),
        (httpx.Response(410, json={"error": "user_erased"}), "erased"),
        (httpx.Response(404, json={"error": "skill_not_found"}), "rejected"),
        (httpx.Response(422, json={"error": "xp_out_of_range"}), "rejected"),
        # A route that is not deployed yet, auth and server trouble are retried.
        (httpx.Response(404, text="Not Found"), "retry"),
        (httpx.Response(401, json={"error": "unauthorized"}), "retry"),
        (httpx.Response(500), "retry"),
        (httpx.Response(503), "retry"),
    ],
)
async def test_responses_are_final_or_retried(challenges: Challenges, reply: httpx.Response, outcome: str) -> None:
    challenges.reply = lambda _: reply
    result, status = await lesson_milestones.send(USER_A, "graded", {"skill_id": SKILL, "xp": 1})
    assert (result, status) == (outcome, reply.status_code)
    challenges.reply = down
    assert await lesson_milestones.send(USER_A, "graded", {"skill_id": SKILL, "xp": 1}) == ("retry", None)


def test_backoff_doubles_up_to_an_hour() -> None:
    waits = [lesson_milestones.backoff(attempt).total_seconds() for attempt in range(1, 10)]
    assert waits == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert lesson_milestones.backoff(10**6).total_seconds() == 3600


async def test_erased_account_stops_delivery_and_export_shows_the_outbox(
    client: httpx.AsyncClient, challenges: Challenges, monkeypatch: MonkeyPatch
) -> None:
    challenges.reply = down
    assert (await client.post("/rooms/graded/complete", json=graded(sign(verdict_claims())))).status_code == 200
    await lesson_milestones.settle()
    async with db_context():
        exported = await export_user_data(USER_A)
    assert [(item["unit_id"], item["xp"], item["state"]) for item in exported.lesson_milestones] == [
        ("graded", 20, "pending")
    ]
    monkeypatch.setattr("api.services.user_deletion.clear_cache", AsyncMock())
    async with db_context():
        await delete_user_data(USER_A)
    challenges.reply = challenges.book
    await make_due()
    await lesson_milestones.recover()
    assert len(challenges.calls) == 1 and await outbox() == []


UNIT: dict[str, Any] = {
    "id": "unit",
    "path_id": "prompting",
    "title": {"de": "Einheit", "en": "Unit"},
    "room": "guided-lesson",
    "content": {},
    "teaches": [],
    "practices": [],
    "requires": [],
    "retired": False,
    "completion": {"kind": "introduced", "answer": {"answer": 6}},
    "milestone": {"skill_id": SKILL, "xp": 20},
}
VIDEO = {"type": "youtube", "id": "dQw4w9WgXcQ"}


@pytest.mark.parametrize(
    "room",
    [
        # Exercises earn XP through their subtask; a video records viewing; no check means no milestone.
        {
            "room": "exercise",
            "completion": None,
            "exercise": {"type": "matching", "task_id": str(uuid4()), "subtask_id": str(uuid4())},
        },
        {
            "room": "video",
            "content": {"de": {"video": VIDEO}, "en": {"video": VIDEO}},
            "completion": {"kind": "introduced", "answer": {"viewed": True}},
        },
        {"room": "custom", "completion": None, "module_id": "llmb-module"},
    ],
)
def test_milestones_need_a_lesson_with_a_server_check(room: dict[str, Any]) -> None:
    assert CatalogueUnit.parse_obj({**UNIT, **room, "milestone": None}).milestone is None
    with pytest.raises(ValidationError):
        CatalogueUnit.parse_obj({**UNIT, **room})


@pytest.mark.parametrize(
    "milestone",
    [
        {"skill_id": SKILL, "xp": 0},
        {"skill_id": SKILL, "xp": 51},
        {"skill_id": SKILL, "xp": True},
        {"skill_id": SKILL, "xp": "20"},
        {"skill_id": "", "xp": 20},
        {"skill_id": "Root Skill", "xp": 20},
        {"skill_id": SKILL, "xp": 20, "coins": 1},
    ],
)
def test_catalogue_rejects_invalid_milestones(milestone: dict[str, Any]) -> None:
    assert CatalogueUnit.parse_obj(UNIT).milestone is not None
    assert CatalogueUnit.parse_obj({**UNIT, "milestone": {"skill_id": SKILL, "xp": 50}}).milestone is not None
    with pytest.raises(ValidationError):
        CatalogueUnit.parse_obj({**UNIT, "milestone": milestone})


def test_milestone_stays_server_side(content: Catalogue) -> None:
    unit = next(unit for unit in content.units if unit.id == "graded")
    assert unit.milestone is not None and "milestone" not in unit.public().dict()
