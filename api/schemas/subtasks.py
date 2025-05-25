from pydantic import BaseModel, Field

from api.utils.docs import example


class SubTask(BaseModel):
    id: str = Field(description="ID of the sub task")
    task_id: str = Field(description="Task ID of the task")
    type: str = Field(description="Type of the sub task")
    creator: str = Field(description="ID of the creator")
    creation_timestamp: str = Field(description="Timestamp of the creation")
    xp: int = Field(description="XP of the sub task")
    coins: int = Field(description="Coins of the sub task")
    solved: bool = Field(description="Whether the sub task is solved")
    rated: bool = Field(description="Whether the sub task is rated")
    enabled: bool = Field(description="Whether the sub task is enabled")
    retired: bool = Field(description="Whether the sub task is retired")

    Config = example(
        id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        task_id="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        type="CODING_CHALLENGE",
        creator="3fa85f64-5717-4562-b3fc-2c963f66afa6",
        creation_timestamp="2024-11-01T21:46:35.626Z",
        xp=0,
        coins=0,
        solved=True,
        rated=True,
        enabled=True,
        retired=True,
    )
