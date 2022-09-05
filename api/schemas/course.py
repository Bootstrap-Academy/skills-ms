from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ValidationError, validator
from pydantic.validators import dict_validator


class LectureType(Enum):
    YOUTUBE = "youtube"
    MP4 = "mp4"


class Lecture(BaseModel):
    name: str
    description: str | None
    type: LectureType

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
    id: str

    @validator("type")
    def _validate_type(cls, v: LectureType) -> LectureType:  # noqa: N805
        if v != LectureType.YOUTUBE:
            raise ValueError
        return v


class Mp4Lecture(Lecture):
    type = LectureType.MP4
    url: str

    @validator("type")
    def _validate_type(cls, v: LectureType) -> LectureType:  # noqa: N805
        if v != LectureType.MP4:
            raise ValueError
        return v


class Section(BaseModel):
    name: str
    description: str | None
    lectures: list[Lecture]


class BaseCourse(BaseModel):
    id: str
    name: str
    description: str | None


class Course(BaseCourse):
    sections: list[Section]


class CourseSummary(BaseCourse):
    sections: int
    lectures: int
