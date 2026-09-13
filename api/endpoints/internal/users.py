from typing import Any

from fastapi import APIRouter, Response

from api.schemas.user_export import UserDataExport
from api.services.user_deletion import delete_user_data
from api.services.user_export import export_user_data
from api.utils.docs import responses


router = APIRouter()


@router.get("/users/{user_id}/export", responses=responses(UserDataExport))
async def export_user(user_id: str) -> UserDataExport:
    """Return all data of a user. Returns empty lists if the user has no data in this service."""

    return await export_user_data(user_id)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str) -> Response:
    """Delete all data of a user."""

    await delete_user_data(user_id)

    return Response(status_code=204)


@router.post("/users/{user_id}/course-rights/{operation}")
async def course_rights(user_id: str, operation: str, body: dict[str, Any]) -> dict[str, Any] | list[Any]:
    """Fixed internal retained-course evidence and exact continuation delivery."""
    from fastapi import HTTPException

    from api.services import retained_rights

    if operation == "list" and body == {}:
        return await retained_rights.list_rights(user_id)
    if operation == "original" and set(body) == {"right_id"} and isinstance(body["right_id"], str):
        return await retained_rights.get_original(user_id, body["right_id"])
    if operation == "deliver" and set(body) == {"grant_id"} and isinstance(body["grant_id"], str):
        return await retained_rights.deliver(user_id, body["grant_id"])
    raise HTTPException(400, "Unsupported course-right operation")
