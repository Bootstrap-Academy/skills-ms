"""Evaluation of a submission of the lesson engine in the sandbox, Sandkasten (Ä33).

skills-ms sends the runner of the submission's revision (main file and parts of its server document) to
`POST {sandkasten_url}/run` in the environment `javascript`, with the job as the run file `job.json`. The runner
answers with one line of JSON: the result body of Ä26 (verdict, criteria, feedback codes, hash of the runs).

Only an answer of 200 whose run ended with status 0 and printed exactly one valid result is a result. Everything
else is technical and leaves the submission pending (§13): another HTTP status (Sandkasten answers a full program
store with 400 compile_error), no connection, a timeout, a killed run, no or broken output, a technical answer of
the runner. A technical failure never becomes a verdict.
"""

import asyncio
import json
import re
from typing import Any

import httpx

from api.settings import settings

TIMEOUT_SECONDS = 15.0
# One evaluation at a time per process: Sandkasten runs two jobs at once for Prod and Test together.
_GATE = asyncio.Semaphore(1)
_HASH = re.compile(r"^[0-9a-f]{64}$")


class SandboxUnavailableError(Exception):
    """No verdict this time; the submission stays pending and a later read tries again."""


def job_of(submission_job: dict[str, Any], goal_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """The job the runner reads (lesson-engine `src/evaluator/job.ts`), from the frozen job of a submission."""
    start = submission_job["publicStart"]
    return {
        "formatVersion": 1,
        "goalId": goal_id,
        "band": submission_job["band"],
        "caseId": start["caseId"],
        "seed": start["seed"],
        "input": plan,
    }


def request_body(document: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    runner = document["runner"]
    return {
        "build": {
            "environment": "javascript",
            "main_file": {"name": runner["main"]["name"], "content": runner["main"]["content"]},
            "files": [{"name": part["name"], "content": part["content"]} for part in runner["parts"]],
        },
        "run": {"files": [{"name": "job.json", "content": json.dumps(job, ensure_ascii=False, separators=(",", ":"))}]},
    }


def _valid(result: Any) -> bool:
    return (
        isinstance(result, dict)
        and set(result) == {"formatVersion", "verdict", "criteria", "feedbackCodes", "runsHash"}
        and result["formatVersion"] == 1
        and result["verdict"] in ("passed", "not_passed")
        and isinstance(result["criteria"], list)
        and all(
            isinstance(item, dict)
            and set(item) == {"id", "passed"}
            and isinstance(item["id"], str)
            and isinstance(item["passed"], bool)
            for item in result["criteria"]
        )
        and isinstance(result["feedbackCodes"], list)
        and all(isinstance(code, str) for code in result["feedbackCodes"])
        and isinstance(result["runsHash"], str)
        and _HASH.match(result["runsHash"]) is not None
    )


def result_of(status_code: int, body: Any) -> dict[str, Any]:
    """The result body of the runner, or SandboxUnavailableError."""
    if status_code != 200 or not isinstance(body, dict):
        raise SandboxUnavailableError(f"sandbox answered {status_code}")
    run = body.get("run")
    if not isinstance(run, dict) or run.get("status") != 0 or not isinstance(run.get("stdout"), str):
        raise SandboxUnavailableError("the run did not end cleanly")
    lines = run["stdout"].strip().split("\n")
    try:
        result = json.loads(lines[0]) if len(lines) == 1 else None
    except ValueError:
        result = None
    if not _valid(result):
        raise SandboxUnavailableError("the runner gave no result")
    return result  # type: ignore[no-any-return]


async def evaluate(
    document: dict[str, Any], job: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    url = settings.sandkasten_url
    if url is None:
        raise SandboxUnavailableError("no sandbox is configured")
    async with _GATE:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
                response = await client.post(url.rstrip("/") + "/run", json=request_body(document, job))
        except httpx.HTTPError as error:
            raise SandboxUnavailableError(f"sandbox unreachable: {type(error).__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = None
    return result_of(response.status_code, body)
