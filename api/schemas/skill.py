from pydantic import BaseModel, Field

from api.utils.docs import example, get_example


class RootSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    dependents: list[str] = Field(description="List of skills that depend on this skill")
    skills: list[str] = Field(description="List of sub skills")
    row: int = Field(description="Row of the skill in the skill tree")
    column: int = Field(description="Column of the skill in the skill tree")
    sub_tree_rows: int = Field(description="Number of rows in the sub skill tree")
    sub_tree_columns: int = Field(description="Number of columns in the sub skill tree")
    icon: str | None = Field(description="Icon of the skill")

    Config = example(
        id="datenbank_experte",
        name="Datenbank-Experte",
        description="Introduction to the skill",
        dependencies=["grundlagen_der_programmierung_und_datenhaltung"],
        dependents=["data_scientist"],
        skills=["mongodb", "postgresql", "mysql", "fortgeschrittene_datenbankmodelle"],
        row=1,
        column=2,
        sub_tree_rows=20,
        sub_tree_columns=20,
        icon="xyz",
    )


class SkillTree(BaseModel):
    skills: list[RootSkill] = Field(description="List of all skills")
    rows: int = Field(description="Number of rows in the skill tree")
    columns: int = Field(description="Number of columns in the skill tree")

    Config = example(skills=get_example(RootSkill), rows=20, columns=20)


class UpdateRootTree(BaseModel):
    rows: int | None = Field(ge=1, lt=1 << 31, description="Number of rows in the skill tree")
    columns: int | None = Field(ge=1, lt=1 << 31, description="Number of columns in the skill tree")


class CreateRootSkill(BaseModel):
    id: str = Field(max_length=256, description="ID of the skill")
    name: str = Field(max_length=256, description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: set[str] = Field(description="List of skill dependencies")
    row: int = Field(ge=0, lt=1 << 31, description="Row of the skill in the skill tree")
    column: int = Field(ge=0, lt=1 << 31, description="Column of the skill in the skill tree")
    sub_tree_rows: int = Field(ge=1, lt=1 << 31, description="Number of rows in the sub skill tree")
    sub_tree_columns: int = Field(ge=1, lt=1 << 31, description="Number of columns in the sub skill tree")
    icon: str | None = Field(max_length=256, description="Icon of the skill")


class UpdateRootSkill(BaseModel):
    name: str | None = Field(max_length=256, description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: set[str] | None = Field(description="List of skill dependencies")
    row: int | None = Field(ge=0, lt=1 << 31, description="Row of the skill in the skill tree")
    column: int | None = Field(ge=0, lt=1 << 31, description="Column of the skill in the skill tree")
    sub_tree_rows: int | None = Field(ge=1, lt=1 << 31, description="Number of rows in the sub skill tree")
    sub_tree_columns: int | None = Field(ge=1, lt=1 << 31, description="Number of columns in the sub skill tree")
    icon: str | None = Field(max_length=256, description="Icon of the skill")


class SubSkill(BaseModel):
    id: str = Field(description="ID of the skill")
    parent_id: str = Field(description="ID of the parent skill")
    name: str = Field(description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: list[str] = Field(description="List of skill dependencies")
    dependents: list[str] = Field(description="List of skills that depend on this skill")
    courses: list[str] = Field(description="List of course ids")
    row: int = Field(description="Row of the skill in the skill tree")
    column: int = Field(description="Column of the skill in the skill tree")
    icon: str | None = Field(description="Icon of the skill")

    Config = example(
        id="datenanalyse_mit_python",
        parent_id="datenanalyse",
        name="Datenanalyse mit Python",
        description="Introduction to the skill",
        dependencies=["algorithmen_zur_datenanalyse"],
        dependents=["datenvisualisierung_mit_python"],
        courses=["datenanalyse_mit_python"],
        row=1,
        column=2,
        icon="xyz",
    )


class SubSkillTree(BaseModel):
    skills: list[SubSkill] = Field(description="List of all sub skills")
    rows: int = Field(description="Number of rows in the sub skill tree")
    columns: int = Field(description="Number of columns in the sub skill tree")

    Config = example(skills=[get_example(SubSkill)], rows=20, columns=20)


class CreateSubSkill(BaseModel):
    id: str = Field(max_length=256, description="ID of the skill")
    name: str = Field(max_length=256, description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: set[str] = Field(description="List of skill dependencies")
    courses: set[str] = Field(description="List of course ids")
    row: int = Field(ge=0, lt=1 << 31, description="Row of the skill in the skill tree")
    column: int = Field(ge=0, lt=1 << 31, description="Column of the skill in the skill tree")
    icon: str | None = Field(max_length=256, description="Icon of the skill")


class UpdateSubSkill(BaseModel):
    name: str | None = Field(max_length=256, description="Name of the skill")
    description: str | None = Field(description="Description of the skill")
    dependencies: set[str] | None = Field(description="List of skill dependencies")
    courses: set[str] | None = Field(description="List of course ids")
    row: int | None = Field(ge=0, lt=1 << 31, description="Row of the skill in the skill tree")
    column: int | None = Field(ge=0, lt=1 << 31, description="Column of the skill in the skill tree")
    icon: str | None = Field(max_length=256, description="Icon of the skill")
