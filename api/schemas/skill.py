from pydantic import BaseModel, Field

from api.utils.docs import example


class RootSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    dependents: list[str] = Field(description="List of skills that depend on this skill")
    skills: list[str] = Field(description="List of sub skills")

    Config = example(
        id="datenbank_experte",
        name="Datenbank-Experte",
        dependencies=["grundlagen_der_programmierung_und_datenhaltung"],
        dependents=["data_scientist"],
        skills=["mongodb", "postgresql", "mysql", "fortgeschrittene_datenbankmodelle"],
    )


class CreateRootSkill(BaseModel):
    id: str = Field(max_length=256, description="ID of the skill")
    name: str = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] = Field(description="List of skill dependencies")


class UpdateRootSkill(BaseModel):
    name: str | None = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] | None = Field(description="List of skill dependencies")


class SubSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    parent_id: str = Field(description="ID of the parent skill")
    name: str = Field(description="Name of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    dependents: list[str] = Field(description="List of skills that depend on this skill")
    courses: list[str] = Field(description="List of course ids")

    Config = example(
        id="datenanalyse_mit_python",
        parent_id="datenanalyse",
        name="Datenanalyse mit Python",
        dependencies=["algorithmen_zur_datenanalyse"],
        dependents=["datenvisualisierung_mit_python"],
        courses=["datenanalyse_mit_python"],
    )


class CreateSubSkill(BaseModel):
    id: str = Field(max_length=256, description="ID of the skill")
    name: str = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] = Field(description="List of skill dependencies")
    courses: set[str] = Field(description="List of course ids")


class UpdateSubSkill(BaseModel):
    name: str | None = Field(max_length=256, description="Name of the skill")
    dependencies: set[str] | None = Field(description="List of skill dependencies")
    courses: set[str] | None = Field(description="List of course ids")
