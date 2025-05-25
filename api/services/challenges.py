from api.schemas.subtasks import SubTask
from api.services.internal import InternalService


async def challenge_subtasks(auth_token: str, solved: bool) -> list[SubTask]:
    async with InternalService.CHALLENGES.client_external(auth_token) as client:
        response = await client.get(f"/subtasks?solved={'true' if solved else 'false'}")
        response.raise_for_status()
        subtasks_data = response.json()
        return [SubTask(**subtask) for subtask in subtasks_data]
