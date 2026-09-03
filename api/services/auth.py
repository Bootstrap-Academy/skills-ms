from typing import cast

from api.schemas.xp import CertificateUser
from api.services.internal import InternalService
from api.utils.cache import redis_cached


async def get_user_status(user_id: str) -> int:
    """Return the status code of the auth microservice for a user without using the cache."""

    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        return response.status_code


@redis_cached("user", "user_id")
async def exists_user(user_id: str) -> bool:
    return await get_user_status(user_id) == 200


@redis_cached("user", "user_id")
async def get_user_for_certificate(user_id: str) -> CertificateUser | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return None

        data = response.json()
        return CertificateUser(
            id=data["id"],
            name=data["name"],
            display_name=data["display_name"],
            email=data["email"],
            avatar_url=data["avatar_url"],
        )


@redis_cached("user", "user_id")
async def get_email(user_id: str) -> str | None:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        if response.status_code != 200:
            return None
        return cast(str | None, response.json()["email"])
