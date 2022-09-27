from pydantic import BaseModel, Field


class SubSkillXP(BaseModel):
    skill: str = Field(description="ID of the skill")
    xp: int = Field(description="Amount of XP the user has in this skill")
    level: int = Field(description="Level of the user in this skill")
    progress: float = Field(ge=0, le=1, description="Progress towards the next level")


class RootSkillXP(BaseModel):
    skill: str = Field(description="ID of the skill")
    xp: int = Field(description="Amount of XP the user has in this skill")
    level: int = Field(description="Level of the user in this skill")
    progress: float = Field(ge=0, le=1, description="Progress towards the next level")
    skills: list[SubSkillXP] = Field(description="List of sub skills")


class XPResponse(BaseModel):
    total_xp: int = Field(description="Total amount of XP the user has")
    total_level: int = Field(description="Total level of the user")
    progress: float = Field(ge=0, le=1, description="Progress towards the next level")
    skills: list[RootSkillXP] = Field(description="List of root skills")
