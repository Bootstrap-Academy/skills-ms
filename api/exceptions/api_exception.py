from fastapi import HTTPException
from fastapi.responses import JSONResponse


class APIException(HTTPException):
    status_code: int
    detail: str
    description: str

    def __init__(self) -> None:
        super().__init__(self.status_code, self.detail)


class CodedAPIException(APIException):
    """An APIException with a stable, machine-readable `code` next to the readable `detail`.

    The body is `{"detail": "<text>", "code": "<code>"}`: clients that matched the text keep working.
    """

    code: str

    def response(self) -> JSONResponse:
        return JSONResponse(
            {"detail": self.detail, "code": self.code}, status_code=self.status_code, headers=self.headers
        )
