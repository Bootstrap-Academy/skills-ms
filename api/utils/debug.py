import json
from typing import Any, Awaitable, Callable, Type

from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, TypeAdapter
from starlette.concurrency import iterate_in_threadpool

from api.logger import get_logger

logger = get_logger(__name__)


def _documented(route: APIRoute, status_code: int) -> dict[str, Any] | None:
    """How OpenAPI describes a status of the route: its entry in `responses`, else, for the route's own success
    status, the `response_model` that FastAPI documents there."""
    if status_code in route.responses:
        return route.responses[status_code]
    if route.response_model is not None and status_code == (route.status_code or 200):
        return {"model": route.response_model}
    return None


def _check_response_schema(method: str, route: APIRoute, status_code: int, body: bytes) -> None:
    if status_code in [405, 422]:
        return
    if not route.include_in_schema:
        return
    response = _documented(route, status_code)
    if response is None:
        logger.error(f"[{method} {route.path}] no response schema defined for status code {status_code}")
        return

    if "model" in response:
        response_schema: Type[BaseModel] = response["model"]
        try:
            TypeAdapter(response_schema).validate_json(body)
        except Exception as e:
            logger.error(f"[{method} {route.path}] response schema validation failed ({status_code}):\n{e}")
    elif json.loads(body) not in (
        v.get("value") for v in response.get("content", {}).get("application/json", {}).get("examples", {}).values()
    ):
        logger.error(f"[{method} {route.path}] response schema validation failed ({status_code})")


async def check_responses(
    request: Request, call_next: Callable[..., Awaitable[StreamingResponse]]
) -> StreamingResponse:
    response: StreamingResponse = await call_next(request)
    if response.headers.get("Content-type") != "application/json":
        return response

    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunks)  # type: ignore

    response.body_iterator = iterate_in_threadpool(iter(chunks))

    if route := request.scope.get("route"):
        _check_response_schema(request.method, route, response.status_code, body)

    return response
