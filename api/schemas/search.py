from pydantic import BaseModel, Field

from api.schemas.course import CourseSummary
from api.schemas.skill import RootSkill, SubSkill


class SearchResults(BaseModel):
    root_skills: list[RootSkill] = Field(description="List of root skills")
    sub_skills: list[SubSkill] = Field(description="List of sub skills")
    courses: list[CourseSummary] = Field(description="List of courses")
