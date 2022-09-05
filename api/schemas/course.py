from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError, validator
from pydantic.validators import dict_validator

from api.utils.docs import example, get_example


class LectureType(Enum):
    YOUTUBE = "youtube"
    MP4 = "mp4"


class Lecture(BaseModel):
    title: str = Field(description="Title of the lecture")
    description: str | None = Field(description="Description of the lecture")
    type: LectureType = Field(description="Type of the lecture")

    @classmethod
    def validate(cls, value: Any) -> Lecture:
        if isinstance(value, cls):
            return value
        data = dict_validator(value)
        for subcls in cls.__subclasses__():
            try:
                return subcls(**data)
            except ValidationError:
                pass
        raise ValueError


class YoutubeLecture(Lecture):
    type = LectureType.YOUTUBE
    id: str = Field(description="Youtube Video ID of the lecture")

    Config = example(
        title="Introduction", description="Introduction to the course", type=LectureType.YOUTUBE, id="dQw4w9WgXcQ"
    )

    @validator("type")
    def _validate_type(cls, v: LectureType) -> LectureType:  # noqa: N805
        if v != LectureType.YOUTUBE:
            raise ValueError
        return v


class Mp4Lecture(Lecture):
    type = LectureType.MP4
    url: str = Field(description="URL of the MP4 file")

    Config = example(
        title="Introduction",
        description="Introduction to the course",
        type=LectureType.MP4,
        url="https://example.com/introduction.mp4",
    )

    @validator("type")
    def _validate_type(cls, v: LectureType) -> LectureType:  # noqa: N805
        if v != LectureType.MP4:
            raise ValueError
        return v


class Section(BaseModel):
    title: str = Field(description="Title of the section")
    description: str | None = Field(description="Description of the section")
    lectures: list[Lecture] = Field(description="Lectures in the section")

    Config = example(
        title="Introduction", description="Introduction to the course", lectures=[get_example(YoutubeLecture)]
    )


class BaseCourse(BaseModel):
    id: str = Field(description="ID of the course")
    title: str = Field(description="Title of the course")
    description: str | None = Field(description="Description of the course")

    Config = example(id="python", title="Python", description="Course description")


class Course(BaseCourse):
    sections: list[Section] = Field(description="Sections in the course")

    Config = example(**get_example(BaseCourse), sections=[get_example(Section)])


class CourseSummary(BaseCourse):
    sections: int = Field(description="Number of sections in the course")
    lectures: int = Field(description="Number of lectures in the course")

    Config = example(**get_example(BaseCourse), sections=42, lectures=1337)
