from pydantic import BaseModel, Field

from api.schemas.skill import RootSkill, SubSkill


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


class UpdateXP(BaseModel):
    xp: int | None = Field(ge=0, description="Amount of XP the user has in this skill")


class CertificateUser(BaseModel):
    id: str = Field(description="Unique identifier for the user")
    name: str = Field(description="Unique username")
    display_name: str = Field(description="Full name of the user")
    email: str = Field(description="Email address of the user")
    avatar_url: str = Field(description="URL of the user's avatar")


# class Certificate(BaseModel):
#     user: CertificateUser = Field(description="User who completed the skill")
#     root_skill: RootSkill = Field(description="Parent skill of the skill that was completed")
#     sub_skill: SubSkill = Field(description="Sub skill that was completed")
