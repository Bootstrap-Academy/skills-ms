"""Refusals of the lesson engine's learner routes, in the error shape of the lesson protocol.

The host decides by `retryable` and `code` alone (BERICHT step 3, host task 1): a 409 is final, an outage or a
5xx may be repeated with the same request id.
"""

from fastapi.responses import JSONResponse

from api.exceptions.api_exception import CodedAPIException


class EngineRefusal(CodedAPIException):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False) -> None:
        self.status_code = status
        self.detail = message
        self.description = message
        self.code = code
        self.retryable = retryable
        super().__init__()

    def response(self) -> JSONResponse:
        body = {"detail": self.detail, "code": self.code, "message": self.detail, "retryable": self.retryable}
        return JSONResponse(body, status_code=self.status_code, headers=self.headers)
