"""Refusals of a change of the learning plan: `detail`, a stable `code` and, where useful, more fields.

A conflict carries the current plan as `plan`; a refused step its position in `steps` as `step`.
"""

from typing import Any

from fastapi.responses import JSONResponse

from api.exceptions.api_exception import CodedAPIException


class PlanRefusal(CodedAPIException):
    def __init__(self, status: int, code: str, message: str, extra: dict[str, Any] | None = None) -> None:
        self.status_code = status
        self.detail = message
        self.description = message
        self.code = code
        self.extra = extra or {}
        super().__init__()
        self.headers = {"Cache-Control": "private, no-store"}

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"detail": self.detail, "code": self.code, **self.extra}, status_code=self.status_code, headers=self.headers
        )
