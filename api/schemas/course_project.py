import json
from datetime import datetime
from typing import Any

from pydantic import validator

from api.schemas.rooms import Mutation, RoomModel


# Compact UTF-8 JSON, the size `JSON.stringify` produces in the browser; larger states get 413.
PROJECT_STATE_LIMIT = 65536


def json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    try:
        # Encoding too: a lone surrogate (valid JSON escape, not UTF-8) is a 422 here, not a 500 later.
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):  # UnicodeEncodeError is a ValueError
        raise ValueError("Invalid JSON state") from None
    return value


def state_size(state: dict[str, Any]) -> int:
    return len(json.dumps(state, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class SaveProject(Mutation):
    state: dict[str, Any]
    _json_state = validator("state", pre=True, allow_reuse=True)(json_object)


class ProjectEnvelope(RoomModel):
    course_id: str
    revision: int
    state: dict[str, Any]
    updated_at: datetime | None
