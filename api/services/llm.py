"""Lesson grants for the LLM gateway (llm-ms) and checks of its signed grading verdicts.

Both are HS256 JWTs in llm-ms' shape (`academy_llm/src/jwt.rs`): the payload is `{exp, ...claims}` with
an integer `exp`. Grant claims follow `GrantClaims` (`academy_llm/src/grant.rs`). Verdicts follow
"Verdict-Format (verbindlich)" in `internal-operations/tasks/LLM-GATEWAY-2026-09-23.md` (llm-ms 157d2e6,
`VerdictClaims` in `academy_llm/src/grading.rs`). Keys are the exact UTF-8 bytes llm-ms uses: a plain
value as given, a credential file without its trailing CR/LF. The gateway never calls this service; a
grant carries the access decision that was made here.

A verdict counts only with its own verdict key: no fallback to the grant key, and like in llm-ms every
key is at least 32 bytes and differs from the others. llm-ms signs nothing in fake mode (`receipt: null`),
which the room treats as "no verdict". Unknown extra claims are ignored as the binding format recommends;
every claim that carries a binding is required and checked.
"""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from time import time
from typing import Any, Literal
from uuid import UUID, uuid4

import jwt
from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationError, validator

from api.logger import get_logger
from api.schemas.rooms import LLM_PROFILE_ID_PATTERN, CatalogueUnit, LlmGrant, LlmVerdictCompletion
from api.settings import settings


GRANT_AUDIENCE = "llm-grant"
VERDICT_AUDIENCE = "llm-verdict"
HEX_SHA256 = r"^[0-9a-f]{64}$"

UNAVAILABLE = "The AI is not available right now"
FOREIGN_VERDICT = "This grading does not belong to this answer"
PRACTICE_VERDICT = "This grading comes from a test mode and does not count"
STALE_VERDICT = "This grading is out of date. Check your answer again."
MIN_KEY_BYTES = 32

logger = get_logger(__name__)


class VerdictClaims(BaseModel):
    """The binding verdict payload; all claims are always present (llm-ms 157d2e6)."""

    class Config:
        # The binding format asks to ignore unknown claims so that an extension does not break the check.
        extra = "ignore"

    aud: Literal["llm-verdict"]
    exp: StrictInt
    iat: StrictInt
    uid: UUID
    unit_id: str
    course_id: str | None
    profile: str = Field(regex=LLM_PROFILE_ID_PATTERN)
    profile_hash: str = Field(regex=HEX_SHA256)
    request_id: UUID
    answer_sha256: str = Field(regex=HEX_SHA256)
    # Language of the grader prompt; the client chooses it, so it is recorded, not bound (see the room).
    locale: Literal["de", "en"]
    score: StrictInt
    max_score: StrictInt
    pass_score: StrictInt
    passed: StrictBool
    model: str = Field(max_length=128)

    @validator("score", "max_score", "pass_score")
    @classmethod
    def points(cls, value: int) -> int:
        if value < 0:
            raise ValueError("Points are never negative")
        return value


def _secret(value: str, file: Path | None) -> bytes | None:
    """The configured bytes (None if unset); ValueError if the configuration cannot be used."""
    if value and file is not None:
        raise ValueError("is configured both as a value and as a file")
    if file is not None:
        try:
            value = file.read_bytes().decode("utf-8").rstrip("\r\n")
        except (OSError, UnicodeDecodeError):
            raise ValueError("file cannot be read") from None
    return value.encode("utf-8") or None


def _key(name: str, value: str, file: Path | None, others: list[bytes]) -> bytes | None:
    try:
        key = _secret(value, file)
    except ValueError as err:
        logger.error("%s %s", name, err)
        return None
    if key is None:
        return None
    if len(key) < MIN_KEY_BYTES:
        logger.error("%s is shorter than %d bytes", name, MIN_KEY_BYTES)
        return None
    if key in others:
        logger.error("%s must differ from every other key", name)
        return None
    return key


def _service_keys() -> list[bytes]:
    names = (
        "jwt_secret",
        "internal_jwt_secret_auth",
        "internal_jwt_secret_shop",
        "internal_jwt_secret_skills",
        "internal_jwt_secret_challenges",
    )
    return [value.encode("utf-8") for value in (getattr(settings, name) for name in names) if value]


def grant_key() -> bytes | None:
    return _key("LLM_GRANT_SECRET", settings.llm_grant_secret, settings.llm_grant_secret_file, _service_keys())


def verdict_key() -> bytes | None:
    """The verdict key. Never the grant key: whoever holds a grant key must not be able to sign a pass."""
    try:
        grant = _secret(settings.llm_grant_secret, settings.llm_grant_secret_file)
    except ValueError:
        grant = None
    others = [*_service_keys(), *([grant] if grant else [])]
    return _key("LLM_VERDICT_SECRET", settings.llm_verdict_secret, settings.llm_verdict_secret_file, others)


def marked_as_practice(payload: dict[str, Any]) -> bool:
    """Whether a verdict says it comes from a fake provider or a test mode.

    llm-ms 157d2e6 signs nothing in fake mode, so this only guards against a future format or a
    misconfigured gateway; such markers are the one exception to ignoring unknown claims.
    """
    return (
        any(payload.get(name) not in (None, False) for name in ("fake", "test"))
        or payload.get("mode") not in (None, "live")
        or payload.get("provider") == "fake"
    )


def answer_sha256(answer: str) -> str:
    """SHA-256 of the exact answer text, hex; llm-ms hashes `input[0].content` the same way."""
    return sha256(answer.encode("utf-8")).hexdigest()


def issue_grant(user_id: str, unit: CatalogueUnit, course_id: str | None) -> LlmGrant:
    """Sign a lesson grant after the caller has checked the learner's access to `unit`."""
    key = grant_key()
    if key is None:
        raise HTTPException(503, UNAVAILABLE)
    try:
        uid = str(UUID(user_id))
    except ValueError:
        raise HTTPException(403, "The AI is not available for this account") from None
    expires = int(time()) + settings.llm_grant_ttl
    profiles = list(unit.llm_profiles)
    claims = {
        "aud": GRANT_AUDIENCE,
        "uid": uid,
        "course_id": course_id,
        "path_id": unit.path_id,
        "unit_id": unit.id,
        "profiles": profiles,
        "jti": str(uuid4()),
        "exp": expires,
    }
    return LlmGrant(
        grant=jwt.encode(claims, key, algorithm="HS256"),
        profiles=profiles,
        expires_at=datetime.fromtimestamp(expires, timezone.utc),
    )


def verify_verdict(
    token: str,
    *,
    user_id: str,
    unit_id: str,
    completion: LlmVerdictCompletion,
    course_id: str | None,
    answer: str,
    not_before: datetime | None = None,
) -> VerdictClaims:
    """Check signature, audience, expiry and every binding of a verdict; raise if anything differs.

    A forged or foreign verdict is a 403. An expired one, one from another rubric version or one that is
    older than the running repeat is a 409: grading the answer again fixes it. Whether it passed is left
    to the caller, so a failing verdict can keep the room open without any cost.
    """
    key = verdict_key()
    if key is None:
        raise HTTPException(503, UNAVAILABLE)
    try:
        payload = jwt.decode(
            token, key, algorithms=["HS256"], audience=VERDICT_AUDIENCE, options={"require": ["exp", "iat", "aud"]}
        )
        if marked_as_practice(payload):
            raise HTTPException(403, PRACTICE_VERDICT)
        claims = VerdictClaims.parse_obj(payload)
    except jwt.ExpiredSignatureError:
        raise HTTPException(409, STALE_VERDICT) from None
    except (jwt.InvalidTokenError, ValidationError):
        raise HTTPException(403, FOREIGN_VERDICT) from None
    try:
        own = claims.uid == UUID(user_id) and claims.answer_sha256 == answer_sha256(answer)
    except ValueError:  # includes UnicodeEncodeError (lone surrogates)
        own = False
    if not own or (claims.unit_id, claims.course_id, claims.profile) != (unit_id, course_id, completion.profile):
        raise HTTPException(403, FOREIGN_VERDICT)
    if claims.profile_hash != completion.profile_sha256:
        raise HTTPException(409, STALE_VERDICT)
    if not_before is not None:
        started = not_before if not_before.tzinfo is not None else not_before.replace(tzinfo=timezone.utc)
        if claims.iat < int(started.timestamp()):
            raise HTTPException(409, STALE_VERDICT)
    return claims
