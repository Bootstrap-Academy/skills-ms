from api.services.internal import InternalService


async def spend_coins(user_id: str, coins: int, description: str) -> bool:
    async with InternalService.SHOP.client as client:
        response = await client.post(f"/coins/{user_id}", json={"coins": -coins, "description": description})
        return response.status_code == 200
