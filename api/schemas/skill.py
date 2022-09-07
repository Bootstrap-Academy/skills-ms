from pydantic import BaseModel, Field

from api.schemas.course import CourseSummary
from api.utils.docs import example


class Skill(BaseModel):
    id: str = Field(description="ID of the skill")
    name: str = Field(description="Name of the skill")
    courses: list[CourseSummary] = Field(description="List of courses")
    instructors: list[None] = Field(description="List of instructors")
    exam_dates: list[None] = Field(description="List of exam dates")
    dependencies: list[str] = Field(description="List of course dependencies")

    Config = example(
        id="software_developer", name="Software Developer", courses=[], instructors=[], dependencies=["web_developer"]
    )
