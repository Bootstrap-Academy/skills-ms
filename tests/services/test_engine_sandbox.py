"""The sandbox client of the lesson engine (Ä33): what it sends, and that only a clean result counts."""

import json
from typing import Any

import httpx
import pytest
from pytest import MonkeyPatch

from api.services import sandbox
from api.settings import settings
from tests.engine_documents import document

JOB: dict[str, Any] = {
    "formatVersion": 1,
    "goalId": "reach",
    "band": "plus",
    "caseId": "open-1",
    "seed": "0" * 32,
    "input": {"kind": "program", "source": "move();"},
}
RESULT = {
    "formatVersion": 1,
    "verdict": "passed",
    "criteria": [{"id": "target", "passed": True}],
    "feedbackCodes": [],
    "runsHash": "f" * 64,
}


SEEN: list[httpx.Request] = []


def answer(status: int = 200, run_status: int = 0, stdout: str | None = None, body: Any = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        SEEN.append(request)
        if body is not None:
            return httpx.Response(status, json=body)
        out = json.dumps(RESULT) + "\n" if stdout is None else stdout
        run = {
            "status": run_status,
            "stdout": out,
            "stderr": "",
            "resource_usage": {"time": 100, "memory": 1000},
            "limits": {},
        }
        return httpx.Response(status, json={"program_id": "x", "ttl": 300, "cached": False, "build": None, "run": run})

    return httpx.MockTransport(handle)


@pytest.fixture(autouse=True)
def configured(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandkasten_url", "http://sandbox.test:8000/")


async def test_the_runner_and_the_job_go_to_run_and_a_clean_result_comes_back() -> None:
    SEEN.clear()
    assert await sandbox.evaluate(document(), JOB, transport=answer()) == RESULT
    [request] = SEEN
    assert (request.method, str(request.url)) == ("POST", "http://sandbox.test:8000/run")
    body = json.loads(request.content)
    assert body["build"]["environment"] == "javascript"
    assert body["build"]["main_file"]["name"] == "main.mjs"
    assert [part["name"] for part in body["build"]["files"]] == ["server-0.txt"]
    assert body["run"]["files"] == [{"name": "job.json", "content": json.dumps(JOB, separators=(",", ":"))}]


def test_the_job_comes_from_the_frozen_job_of_a_submission() -> None:
    frozen = {
        "revisionId": "r",
        "publicStart": {"caseId": "open-1", "seed": "0" * 32, "startId": "s"},
        "band": "plus",
        "serverSha256": "a" * 64,
    }
    assert sandbox.job_of(frozen, "reach", JOB["input"]) == JOB


@pytest.mark.parametrize(
    "transport",
    [
        answer(status=500, body={"error": "internal_server_error"}),
        answer(status=400, body={"error": "compile_error"}),
        answer(run_status=1),
        answer(run_status=137, stdout=""),
        answer(stdout=""),
        answer(stdout=json.dumps(RESULT) + "\n" + json.dumps(RESULT) + "\n"),
        answer(stdout='{"formatVersion": 1, "technical": "runner: the parts do not match the server bundle"}\n'),
        answer(stdout=json.dumps({**RESULT, "verdict": "maybe"}) + "\n"),
        answer(stdout=json.dumps({**RESULT, "runs": []}) + "\n"),
        answer(stdout="not json\n"),
    ],
)
async def test_anything_but_a_clean_result_is_technical(transport: httpx.MockTransport) -> None:
    with pytest.raises(sandbox.SandboxUnavailableError):
        await sandbox.evaluate(document(), JOB, transport=transport)


async def test_no_connection_a_timeout_or_no_sandbox_is_technical(monkeypatch: MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (refuse, slow):
        with pytest.raises(sandbox.SandboxUnavailableError):
            await sandbox.evaluate(document(), JOB, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(settings, "sandkasten_url", None)
    with pytest.raises(sandbox.SandboxUnavailableError, match="no sandbox"):
        await sandbox.evaluate(document(), JOB, transport=answer())
