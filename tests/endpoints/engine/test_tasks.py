"""Lesson engine, PR 3: start, submission and reading a submission (§9, Ä23, Ä28, Ä32a, Ä37), the save limit (K5)
and a fixed curriculum while units of the engine are unavailable (Ä38 a)."""

import asyncio
import time
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pytest import MonkeyPatch

from api.schemas.course import Course
from api.schemas.curriculum import CurriculumDefinition
from api.schemas.rooms import Catalogue
from api.services import curriculum
from api.services import engine as engine_service
from api.settings import settings
from tests.endpoints.engine.support import LEARNER as LEARNER_ID
from tests.endpoints.engine.support import publish
from tests.engine_documents import PART, UNIT, document, request

LEARNER = {"Authorization": "Bearer learner"}
PLAN = {"kind": "program", "source": "while (frontIsClear()) move();"}


async def save(client: httpx.AsyncClient, plans: dict[str, Any], revision: int) -> int:
    state = {"formatVersion": 1, "activeGoalId": "reach", "plans": plans}
    response = await client.put(
        f"/rooms/{UNIT}/state",
        json={"request_id": str(uuid4()), "expected_revision": revision, "state": state},
        headers=LEARNER,
    )
    assert response.status_code == 200, response.text
    return int(response.json()["progress"]["revision"])


async def start(
    client: httpx.AsyncClient, revision_id: str, request_id: str = "start-1", goal: str = "reach"
) -> httpx.Response:
    return await client.post(
        f"/engine/units/{UNIT}/tasks/{goal}/start",
        json={"requestId": request_id, "revisionId": revision_id},
        headers=LEARNER,
    )


async def submit(
    client: httpx.AsyncClient, start_id: str, state_revision: int, plan: Any = PLAN, request_id: str = "submit-1"
) -> httpx.Response:
    return await client.post(
        f"/engine/units/{UNIT}/tasks/reach/submissions",
        json={"requestId": request_id, "stateRevision": state_revision, "answer": {"startId": start_id, "input": plan}},
        headers=LEARNER,
    )


def refusal(response: httpx.Response) -> tuple[int, str, bool]:
    body = response.json()
    return response.status_code, body["code"], body["retryable"]


async def published(client: httpx.AsyncClient) -> str:
    return str((await publish(client, request())).json()["revision_id"])


async def test_a_start_is_drawn_once_and_given_again_until_it_is_used(client: httpx.AsyncClient) -> None:
    revision_id = await published(client)
    first = await start(client, revision_id)
    assert first.status_code == 200, first.text
    body = first.json()
    public = body["start"]
    # The initial band is "plus": its practice case, a seed of 128 bits, nothing of the band itself.
    assert (public["revisionId"], public["goalId"], public["caseId"], public["config"]) == (
        revision_id,
        "reach",
        "open-1",
        {"width": 4},
    )
    assert len(public["seed"]) == 32 and set(public["seed"]) <= set("0123456789abcdef")
    assert set(public) == {"startId", "revisionId", "goalId", "caseId", "config", "initial", "seed"}
    assert body["assessments"] == []
    # The same request again, and another request on the open start: the same start.
    assert (await start(client, revision_id)).json()["start"] == public
    assert (await start(client, revision_id, request_id="start-2")).json()["start"] == public


async def test_a_start_is_refused_for_another_revision_an_unknown_task_or_a_unit_of_another_kind(
    client: httpx.AsyncClient,
) -> None:
    revision_id = await published(client)
    assert refusal(await start(client, str(uuid4()))) == (409, "revision_changed", False)
    assert refusal(await start(client, revision_id, goal="fly")) == (404, "not_found", False)
    plain = await client.post(
        "/engine/units/plain/tasks/reach/start", json={"requestId": "s", "revisionId": revision_id}, headers=LEARNER
    )
    assert refusal(plain) == (404, "not_found", False)
    too_long = await start(client, revision_id, request_id="r" * 101)
    assert too_long.status_code == 422


async def test_the_saved_plan_is_submitted_once_and_read_back(client: httpx.AsyncClient) -> None:
    revision_id = await published(client)
    state_revision = await save(client, {"reach": PLAN}, 0)
    start_id = (await start(client, revision_id)).json()["start"]["startId"]

    accepted = await submit(client, start_id, state_revision)
    assert accepted.status_code == 202, accepted.text
    view = accepted.json()
    assert set(view) == {"assessmentId", "goalId", "status"} and view["status"] == "pending"
    # §9: the same plan for the same start is the same submission, whatever request id carries it (Ä32a).
    again = await submit(client, start_id, state_revision, request_id="submit-other")
    assert (again.status_code, again.json()) == (202, view)
    # Ä37: another plan on this start needs the next start.
    other = await save(client, {"reach": {"kind": "program", "source": "move();"}}, state_revision)
    assert refusal(await submit(client, start_id, other, plan={"kind": "program", "source": "move();"})) == (
        409,
        "start_required",
        False,
    )
    # The pending submission keeps the start, and a start answer names it (Ä23).
    reopened = (await start(client, revision_id, request_id="start-3")).json()
    assert (reopened["start"]["startId"], reopened["assessments"]) == (start_id, [view])
    assert (await client.get(f"/engine/submissions/{view['assessmentId']}", headers=LEARNER)).json() == view
    stranger = await client.get(
        f"/engine/submissions/{view['assessmentId']}", headers={"Authorization": "Bearer admin"}
    )
    assert refusal(stranger) == (404, "not_found", False)


async def test_only_the_confirmed_plan_of_the_open_start_can_be_submitted(client: httpx.AsyncClient) -> None:
    revision_id = await published(client)
    assert refusal(await submit(client, str(uuid4()), 0)) == (409, "start_required", False)
    state_revision = await save(client, {"reach": PLAN}, 0)
    start_id = (await start(client, revision_id)).json()["start"]["startId"]
    assert refusal(await submit(client, str(uuid4()), state_revision)) == (409, "start_required", False)
    # Another plan than the saved one, or a revision of the room that is not the current one.
    assert refusal(await submit(client, start_id, state_revision, plan={"kind": "program", "source": "move();"})) == (
        409,
        "conflict",
        False,
    )
    assert refusal(await submit(client, start_id, state_revision - 1)) == (409, "conflict", False)


@pytest.mark.parametrize(
    "plan",
    [
        {"kind": "program"},
        {"kind": "program", "source": "x" * 65537},
        {"kind": "program", "source": 5},
        {"kind": "script", "source": "move();"},
        {"kind": "actions", "actions": [{"name": "move", "target": "robot", "args": {}, "actor": "robot", "tick": -1}]},
        {
            "kind": "actions",
            "actions": [{"name": "move", "target": "robot", "args": {}, "actor": "robot", "tick": 0, "x": 1}],
        },
        {"kind": "actions", "actions": [{"step": False, "tick": 0}]},
        {
            "kind": "actions",
            "actions": [{"name": "move", "target": "robot", "args": {"n": 1.5}, "actor": "robot", "tick": 0}],
        },
    ],
)
async def test_a_plan_of_another_form_is_refused(client: httpx.AsyncClient, plan: Any) -> None:
    revision_id = await published(client)
    start_id = (await start(client, revision_id)).json()["start"]["startId"]
    response = await submit(client, start_id, 0, plan=plan)
    assert response.status_code == 422, response.text


async def test_a_plan_of_the_other_kind_than_the_task_takes_is_refused(client: httpx.AsyncClient) -> None:
    # The task "reach" takes a program. A list of direct actions is no answer to it, even when it is the saved plan:
    # a list may name any actor and wait empty ticks, which a program of the editor cannot.
    revision_id = await published(client)
    actions = {
        "kind": "actions",
        "actions": [{"name": "move", "target": "robot", "args": {}, "actor": "robot", "tick": 0}],
    }
    state_revision = await save(client, {"reach": actions}, 0)
    start_id = (await start(client, revision_id)).json()["start"]["startId"]
    assert refusal(await submit(client, start_id, state_revision, plan=actions)) == (422, "plan_kind", False)
    # Nothing was taken: the same start takes the program the learner saves next.
    program = await save(client, {"reach": PLAN}, state_revision)
    accepted = await submit(client, start_id, program, request_id="submit-2")
    assert accepted.status_code == 202, accepted.text


async def test_a_new_revision_gives_a_new_start_unless_a_submission_is_pending(client: httpx.AsyncClient) -> None:
    first = await published(client)
    state_revision = await save(client, {"reach": PLAN}, 0)
    old = (await start(client, first)).json()["start"]
    second = (await publish(client, request(document(parts=(PART, "// 2\n")), package_hash="d" * 64))).json()[
        "revision_id"
    ]
    # Without a pending submission: a new start on the new revision, in the same band; the plan stays saved.
    new = (await start(client, second, request_id="start-2")).json()["start"]
    assert (new["revisionId"], new["caseId"]) == (second, old["caseId"]) and new["startId"] != old["startId"]
    accepted = await submit(client, new["startId"], state_revision)
    assert accepted.status_code == 202
    # With one pending on the second revision, a third revision waits for its verdict.
    third = (await publish(client, request(document(parts=(PART, "// 3\n")), package_hash="e" * 64))).json()[
        "revision_id"
    ]
    waiting = await start(client, third, request_id="start-3")
    # Ä40: a 409 is final, and this one names the pending submission the host can follow up.
    assert refusal(waiting) == (409, "pending", False)
    assert waiting.json()["assessments"] == [accepted.json()]


async def test_saves_of_a_unit_of_the_engine_are_limited_per_window(
    client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    await published(client)
    counts: dict[str, int] = {}

    async def count(key: str) -> int:
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    monkeypatch.setattr(engine_service, "count_save", count)
    revision = 0
    for _ in range(engine_service.SAVES_PER_WINDOW):
        revision = await save(client, {"reach": PLAN}, revision)
    refused = await client.put(
        f"/rooms/{UNIT}/state",
        json={"request_id": str(uuid4()), "expected_revision": revision, "state": {"plans": {}}},
        headers=LEARNER,
    )
    assert (refused.status_code, refused.headers.get("retry-after")) == (429, "30")
    # Saves of other units are not counted.
    assert (
        await client.put(
            "/rooms/plain/state",
            json={"request_id": str(uuid4()), "expected_revision": 0, "state": {"a": 1}},
            headers=LEARNER,
        )
    ).status_code == 200
    assert list(counts) == [f"engine-saves:{LEARNER_ID}:{UNIT}"]

    async def hanging(key: str) -> int:
        await asyncio.sleep(5)
        return 0

    # A Redis that accepts and does not answer: the save goes through after a fifth of a second (P3-1).
    monkeypatch.setattr(engine_service, "count_save", hanging)
    began = time.monotonic()
    revision = await save(client, {"reach": PLAN}, revision)
    assert time.monotonic() - began < 2

    async def broken(key: str) -> int:
        raise OSError("no redis")

    # Without Redis the limit steps aside.
    monkeypatch.setattr(engine_service, "count_save", broken)
    assert (await save(client, {"reach": PLAN}, revision)) == revision + 1


def course(units: list[str]) -> Course:
    lessons = [
        {
            "id": f"lesson-{uid}",
            "title": {"de": uid, "en": uid},
            "activities": [{"id": uid, "source": {"kind": "room", "unit_id": uid}}],
        }
        for uid in units
    ]
    return Course(
        id="engine-course",
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
        learning_path_id="engine-path",
        curriculum=CurriculumDefinition.model_validate({"lessons": lessons}),
    )


def test_a_fixed_curriculum_leaves_out_an_unavailable_unit_of_the_engine(
    catalogue: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    fixed = course([UNIT, "plain"])
    # Switched off: the lesson of the engine is left out, the course stays available (Ä38 a).
    monkeypatch.setattr(settings, "engine_enabled", False)
    definition, _ = curriculum.definitions(fixed)
    assert [lesson.id for lesson in definition.lessons] == ["lesson-plain"]
    # On, but without an active revision: the same.
    monkeypatch.setattr(settings, "engine_enabled", True)
    assert [lesson.id for lesson in curriculum.definitions(fixed)[0].lessons] == ["lesson-plain"]
    # On and published: the lesson is there.
    monkeypatch.setattr(engine_service, "available", lambda item: item.id == UNIT)
    assert [lesson.id for lesson in curriculum.definitions(fixed)[0].lessons] == [f"lesson-{UNIT}", "lesson-plain"]


async def test_a_start_answer_names_pending_submissions_and_counted_passes_only(client: httpx.AsyncClient) -> None:
    from api.database import db, db_context
    from tests.engine_rows import start_id, submission, task_state

    revision_id = await published(client)
    counted = str(uuid4())
    async with db_context():
        await db.add(task_state(LEARNER_ID, revision_id=revision_id, start_id=None, start=None, start_band=None))
        await db.session.flush()
        await db.add(
            submission(LEARNER_ID, str(uuid4()), revision_id=revision_id, start_id=start_id(1), input_hash="1" * 64)
        )
        await db.add(
            submission(
                LEARNER_ID,
                counted,
                revision_id=revision_id,
                start_id=start_id(2),
                input_hash="2" * 64,
                verdict="passed",
                counts=True,
            )
        )
        await db.session.flush()
    body = (await start(client, revision_id)).json()
    # The fail is not named; the pass is, and the new start answers next to it (P3-4 of the review of #431).
    assert [(view["assessmentId"], view["status"], view["outcome"]) for view in body["assessments"]] == [
        (counted, "final", "passed")
    ]
