"""Synthetic server documents in the lesson engine's format 1 (`lesson-engine` `src/build/server-document.ts`).

The runner here is a stand-in of a few bytes: this service stores and hashes it, it never runs it.
"""

from hashlib import sha256
from typing import Any

from api.utils.canonical import digest

UNIT = "engine-unit"
SUBSKILL = "engine-subskill"
PART = 'export const assess = (job) => ({ formatVersion: 1, technical: "synthetic" });\n'


def document(
    unit_id: str = UNIT,
    *,
    xp: int = 1,
    subskill: str = SUBSKILL,
    required: bool = True,
    parts: tuple[str, ...] = (PART,),
    goal: str = "reach",
) -> dict[str, Any]:
    practice = {
        "base": {"caseId": "open-0", "config": {"width": 3}, "initial": {"robot": {"x": 0}}},
        "plus": {"caseId": "open-1", "config": {"width": 4}, "initial": {"robot": {"x": 0}}},
    }
    return {
        "formatVersion": 1,
        "engine": "0.6.0",
        "unitId": unit_id,
        "kernel": {"id": "arena", "version": "1"},
        "bandRule": {"bands": ["base", "plus"], "initial": "plus", "lowerAfter": 2, "raiseAfter": 2},
        "tasks": [
            {
                "goalId": goal,
                "subskillId": subskill,
                "concepts": ["move"],
                "xp": xp,
                "required": required,
                "input": "program",
                "criteria": ["target"],
                "feedback": {"boundary": {"de": "Prüf auch andere Weglängen."}},
                "practice": practice,
            }
        ],
        "runner": {
            "main": {"name": "main.mjs", "content": '// runner\nconst PARTS = ["server-0.txt"];\n'},
            "parts": [{"name": f"server-{index}.txt", "content": part} for index, part in enumerate(parts)],
            # UTF-8 equals ASCII for real parts; a test may hand in a part that is not ASCII.
            "sha256": sha256("".join(parts).encode("utf-8")).hexdigest(),
        },
    }


def request(server_document: dict[str, Any] | None = None, **changes: Any) -> dict[str, Any]:
    """The body of the admin route for a document; `server_sha256` is its canonical hash unless changed."""
    server_document = document() if server_document is None else server_document
    return {
        "unit_id": server_document["unitId"],
        "package_hash": "a" * 64,
        "manifest_hash": "b" * 64,
        "entry_path": "index.html",
        "evidence_sha256": "c" * 64,
        "server_sha256": changes.pop("server_sha256") if "server_sha256" in changes else digest(server_document),
        "server_document": server_document,
        **changes,
    }
