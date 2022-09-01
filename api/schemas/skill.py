from pydantic import BaseModel

from api.utils.docs import example


class Skill(BaseModel):
    id: str
    name: str
    courses: list[None]
    instructors: list[None]
    exam_dates: list[None]
    dependencies: list[str]

    Config = example(
        id="software_developer", name="Software Developer", courses=[], instructors=[], dependencies=["web_developer"]
    )
