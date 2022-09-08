from api.services.internal import auth_client


async def exists_user(user_id: str) -> bool:
    async with auth_client() as client:
        response = await client.get(f"/users/{user_id}")
        return response.status_code == 200
