from fastapi import APIRouter, Response

from api.services.user_deletion import delete_user_data


router = APIRouter()


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: str) -> Response:
    """Delete all data of a user."""

    await delete_user_data(user_id)

    return Response(status_code=204)
