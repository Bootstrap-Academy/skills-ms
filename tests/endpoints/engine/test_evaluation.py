"""Lesson engine, PR 4: evaluation of a submission in the sandbox and booking of its verdict through the routes
(§9, §13, Ä28, Ä33), and the confirmation of a completed unit in the room route."""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pytest import MonkeyPatch

from api import models
from api.database import db, db_context
from api.services import rooms, sandbox
from api.services.engine_verdicts import xp_operation_id
from api.services.sandbox import SandboxUnavailableError
from api.settings import settings
from api.utils.utc import utcnow
from tests.endpoints.engine.support import LEARNER as LEARNER_ID
from tests.endpoints.engine.test_tasks import LEARNER, PLAN, course, published, save, start, submit
from tests.engine_documents import SUBSKILL, UNIT, document

PASS = {
    "formatVersion": 1,
    "verdict": "passed",
    "criteria": [{"id": "target", "passed": True}],
    "feedbackCodes": [],
    "runsHash": "f" * 64,
}
FAIL = {**PASS, "verdict": "not_passed", "criteria": [{"id": "target", "passed": False}], "feedbackCodes": ["boundary"]}


class Sandbox:
    """Stands in for `sandbox.evaluate`: answers the queued results in order; an exception is raised."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, server_document: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((server_document, job))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


@pytest.fixture
def accounts(monkeypatch: MonkeyPatch) -> AsyncMock:
    status = AsyncMock(return_value=200)
    monkeypatch.setattr("api.services.benefits.get_user_status", status)
    monkeypatch.setattr(settings, "sandkasten_url", "http://sandbox.test")
    return status


def runs(monkeypatch: MonkeyPatch, *answers: Any) -> Sandbox:
    fake = Sandbox(*answers)
    monkeypatch.setattr(sandbox, "evaluate", fake)
    return fake


async def submitted(client: httpx.AsyncClient) -> tuple[str, dict[str, Any], int, httpx.Response]:
    """Publishes, saves the plan, starts and submits; the revision, the public start, the room's revision and the
    answer of the submission."""
    revision_id = await published(client)
    state_revision = await save(client, {"reach": PLAN}, 0)
    public = (await start(client, revision_id)).json()["start"]
    return revision_id, public, state_revision, await submit(client, public["startId"], state_revision)


async def room(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.get(f"/rooms/{UNIT}", headers=LEARNER)
    assert response.status_code == 200, response.text
    return dict(response.json()["progress"])


async def complete(client: httpx.AsyncClient, revision: int) -> httpx.Response:
    return await client.post(
        f"/rooms/{UNIT}/complete",
        json={"request_id": str(uuid4()), "expected_revision": revision, "action": "complete"},
        headers=LEARNER,
    )


async def test_a_pass_is_booked_in_the_answer_of_the_submission_and_completes_the_room(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, accounts: AsyncMock
) -> None:
    fake = runs(monkeypatch, PASS)
    revision_id, public, state_revision, answer = await submitted(client)
    assert answer.status_code == 200, answer.text
    view = answer.json()
    operation = xp_operation_id(LEARNER_ID, UNIT, "reach")
    assert (view["status"], view["outcome"], view["counts"], view["completed"], view["xp"]) == (
        "final",
        "passed",
        True,
        True,
        {"awardId": operation, "xp": 1, "persisted": True},
    )
    # The sandbox got the runner of the bound revision and the frozen job: band, public case, seed and plan.
    [(server_document, job)] = fake.calls
    assert server_document == document()
    assert job == {
        "formatVersion": 1,
        "goalId": "reach",
        "band": "plus",
        "caseId": public["caseId"],
        "seed": public["seed"],
        "input": PLAN,
    }
    # XP stand only in the answer that booked them (Ä28); reading again and the next start name the pass without.
    read = await client.get(f"/engine/submissions/{view['assessmentId']}", headers=LEARNER)
    without = {key: value for key, value in view.items() if key != "xp"}
    assert read.json() == without
    again = (await start(client, revision_id, request_id="start-2")).json()
    assert again["assessments"] == [without] and again["start"]["startId"] != public["startId"]
    async with db_context():
        assert await models.XP.get_user_skill_xp(LEARNER_ID, SUBSKILL) == 1
    # The room is completed in the booking, with the revision of the last save.
    progress = await room(client)
    assert (progress["status"], progress["result"], progress["revision"]) == (
        "completed",
        {"kind": "passed"},
        state_revision,
    )
    # The room route confirms the booked pass and writes nothing.
    confirmed = await complete(client, state_revision)
    assert confirmed.status_code == 200, confirmed.text
    assert (confirmed.json()["progress"]["status"], confirmed.json()["progress"]["revision"]) == (
        "completed",
        state_revision,
    )
    assert (await room(client))["revision"] == state_revision
    # A save after the booking keeps the completion.
    assert await save(client, {"reach": PLAN}, state_revision) == state_revision + 1
    assert (await room(client))["status"] == "completed"
    assert len(fake.calls) == 1


async def test_a_completed_unit_offers_no_practice_round_and_a_passed_task_is_played_again(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, accounts: AsyncMock
) -> None:
    # Browser run on Test, F1: the reopened unit offered a practice round, `…/review` refused it with 409, and the
    # page showed a conflict instead of the unit.
    linked = course([UNIT, "plain"])
    monkeypatch.setattr(rooms, "COURSES", {linked.id: linked})
    runs(monkeypatch, PASS, PASS)
    revision_id, _, state_revision, answer = await submitted(client)
    assert answer.json()["completed"] is True
    for route in (
        f"/rooms/{UNIT}?course={linked.id}",
        f"/rooms?path=engine-path&course={linked.id}&unit={UNIT}&continuous=true",
        # The stream repeats completed units after the new ones.
        "/rooms?path=engine-path&after=plain&continuous=true",
    ):
        body = (await client.get(route, headers=LEARNER)).json()
        envelope = body.get("next", body)
        assert (envelope["unit"]["id"], envelope["progress"]["status"], envelope["review_available"]) == (
            UNIT,
            "completed",
            False,
        ), route
    # Ä43: the unit itself plays a passed task again. A new start, a save, a booked pass without XP; the completion
    # stays.
    again = (await start(client, revision_id, request_id="start-2")).json()["start"]
    revision = await save(client, {"reach": PLAN}, state_revision)
    replay = await submit(client, again["startId"], revision, request_id="submit-2")
    assert replay.status_code == 200, replay.text
    view = replay.json()
    assert (view["outcome"], view["counts"], view["completed"], "xp" in view) == ("passed", True, True, False)
    progress = await room(client)
    assert (progress["status"], progress["result"], progress["revision"]) == ("completed", {"kind": "passed"}, revision)
    async with db_context():
        assert await models.XP.get_user_skill_xp(LEARNER_ID, SUBSKILL) == 1


async def test_without_a_verdict_the_submission_stays_pending_until_a_read_after_the_lease(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, accounts: AsyncMock
) -> None:
    fake = runs(monkeypatch, SandboxUnavailableError("sandbox answered 500"), PASS)
    _, _, state_revision, answer = await submitted(client)
    assert (answer.status_code, answer.json()["status"]) == (202, "pending")
    assessment = answer.json()["assessmentId"]
    # While the lease of the failed evaluation runs, a read evaluates nothing.
    assert (await client.get(f"/engine/submissions/{assessment}", headers=LEARNER)).json()["status"] == "pending"
    assert len(fake.calls) == 1
    # The room route refuses while nothing is booked.
    assert (await complete(client, state_revision)).status_code == 409
    async with db_context():
        row = await db.get(models.EngineSubmission, id=assessment)
        assert row is not None and row.state == "accepted" and row.evaluating_until is not None
        row.evaluating_until = utcnow() - timedelta(seconds=1)
    # Once it has passed, the next read evaluates again and books.
    read = (await client.get(f"/engine/submissions/{assessment}", headers=LEARNER)).json()
    assert (read["status"], read["outcome"], "xp" in read, len(fake.calls)) == ("final", "passed", True, 2)


async def test_a_fail_shows_the_texts_of_the_package_and_the_next_start_is_a_new_one(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, accounts: AsyncMock
) -> None:
    runs(monkeypatch, FAIL)
    revision_id, public, _, answer = await submitted(client)
    view = answer.json()
    assert (answer.status_code, view["outcome"], view["counts"], "xp" in view, "completed" in view) == (
        200,
        "not_passed",
        False,
        False,
        False,
    )
    assert view["feedback"] == {
        "source": "server",
        "summary": "Probier es noch einmal.",
        "public_reasons": ["Prüf auch andere Weglängen."],
    }
    # The start is used up; the next one is new and names no fail (Ä23 names counted passes only).
    following = (await start(client, revision_id, request_id="start-2")).json()
    assert following["start"]["startId"] != public["startId"] and following["assessments"] == []
    assert (await room(client))["status"] == "in_progress"


async def test_a_booking_that_fails_leaves_the_submission_pending(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch, accounts: AsyncMock
) -> None:
    # The account service cannot confirm the learner: apply_xp refuses, and nothing is booked.
    accounts.return_value = 503
    runs(monkeypatch, PASS)
    _, _, _, answer = await submitted(client)
    assert (answer.status_code, answer.json()["status"]) == (202, "pending")
    async with db_context():
        row = await db.get(models.EngineSubmission, id=answer.json()["assessmentId"])
        assert row is not None and row.state == "accepted"
        assert await models.XP.get_user_skill_xp(LEARNER_ID, SUBSKILL) == 0


async def test_without_a_sandbox_a_submission_waits_and_takes_no_lease(client: httpx.AsyncClient) -> None:
    assert settings.sandkasten_url is None
    _, _, _, answer = await submitted(client)
    assert (answer.status_code, answer.json()["status"]) == (202, "pending")
    async with db_context():
        row = await db.get(models.EngineSubmission, id=answer.json()["assessmentId"])
        assert row is not None and row.evaluating_until is None
