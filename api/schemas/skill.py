from pydantic import BaseModel, Field


class SubSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    courses: list[str] = Field(description="List of course ids")
    coaches: list[None] = Field(description="List of coaches")
    exam_dates: list[None] = Field(description="List of exam dates")
    webinars: list[None] = Field(description="List of webinars")


class RootSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    skills: list[str] = Field(description="List of sub skills")
