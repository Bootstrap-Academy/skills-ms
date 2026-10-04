"""Refusals of an LLM-graded completion. Each carries a stable `code` next to its unchanged `detail` text.

Codes that ask for a new grading of the same answer: `verdict_stale`, `verdict_used`, `verdict_foreign`,
`verdict_practice`.
"""

from starlette import status

from api.exceptions.api_exception import CodedAPIException


class VerdictRequiredError(CodedAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "Send your answer together with its grading"
    description = "An AI-graded room completes with `{text}` and its signed verdict, or with the fallback answer."
    code = "verdict_required"


class VerdictUnexpectedError(CodedAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "This room is not graded by the AI"
    description = "A verdict was sent for a room that is not graded by the AI."
    code = "verdict_unexpected"


class VerdictFailedError(CodedAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    detail = "Check your answer and try again"
    description = "The verdict is valid but did not pass; the room stays open."
    code = "verdict_failed"


class ForeignVerdictError(CodedAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "This grading does not belong to this answer"
    description = "The verdict is forged, malformed or bound to another learner, unit, course, profile or answer."
    code = "verdict_foreign"


class PracticeVerdictError(CodedAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "This grading comes from a test mode and does not count"
    description = "The verdict says it comes from a fake provider or a test mode."
    code = "verdict_practice"


class StaleVerdictError(CodedAPIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "This grading is out of date. Check your answer again."
    description = "The verdict expired, comes from another rubric version or is older than the running repeat."
    code = "verdict_stale"


class UsedVerdictError(CodedAPIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "This grading was already used. Check your answer again."
    description = "Every verdict completes a room once."
    code = "verdict_used"


class VerdictUnavailableError(CodedAPIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    detail = "The AI is not available right now"
    description = "No usable verdict key is configured."
    code = "verdict_unavailable"
