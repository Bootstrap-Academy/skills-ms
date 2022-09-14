from pydantic import BaseModel, Field

from api.utils.docs import example, get_example


class SubSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    courses: list[str] = Field(description="List of course ids")

    Config = example(
        id="datenanalyse_mit_python",
        name="Datenanalyse mit Python",
        dependencies=["algorithmen_zur_datenanalyse"],
        courses=["datenanalyse_mit_python"],
    )


class SubSkillExtended(SubSkill):
    coaches: list[None] = Field(description="List of coaches")
    exam_dates: list[None] = Field(description="List of exam dates")
    webinars: list[None] = Field(description="List of webinars")

    Config = example(**get_example(SubSkill), coaches=[], exam_dates=[], webinars=[])


class RootSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    dependents: list[str] = Field(description="List of skills that depend on this skill")
    skills: list[str] = Field(description="List of sub skills")

    Config = example(
        id="data_scientist", name="Data Scientist", dependencies=["algorithmiker", "datenbank_experte"], dependents=[]
    )


class CreateRootSkill(BaseModel):
    id: str = Field(max_length=256, description="ID of the skill")
    name: str = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] = Field(description="List of skill dependencies")


class UpdateRootSkill(BaseModel):
    name: str | None = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] | None = Field(description="List of skill dependencies")
