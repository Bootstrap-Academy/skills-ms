from api.schemas.xp import CertificateUser
from api.services.internal import InternalService


async def exists_user(user_id: str) -> bool:
    async with InternalService.AUTH.client as client:
        response = await client.get(f"/users/{user_id}")
        return response.status_code == 200


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
