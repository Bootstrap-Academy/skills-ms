from __future__ import annotations

from pydantic import BaseModel, Field

from api.utils.docs import example, get_example


class YoutubeLecture(BaseModel):
    title: str = Field(description="Title of the lecture")
    description: str | None = Field(description="Description of the lecture")
    type = Field("youtube", const=True, description="Type of the lecture")
    video_id: str = Field(description="Youtube Video ID of the lecture")

    Config = example(
        title="Introduction", description="Introduction to the course", type="youtube", video_id="dQw4w9WgXcQ"
    )


class Mp4Lecture(BaseModel):
    title: str = Field(description="Title of the lecture")
    description: str | None = Field(description="Description of the lecture")
    type = Field("mp4", const=True, description="Type of the lecture")
    video_url: str = Field(description="URL of the MP4 file")

    Config = example(
        title="Introduction",
        description="Introduction to the course",
        type="mp4",
        video_url="https://example.com/introduction.mp4",
    )


Lecture = YoutubeLecture | Mp4Lecture


class Section(BaseModel):
    title: str = Field(description="Title of the section")
    description: str | None = Field(description="Description of the section")
    lectures: list[Lecture] = Field(description="Lectures in the section")

    Config = example(
        title="Introduction",
        description="Introduction to the course",
        lectures=[get_example(YoutubeLecture), get_example(Mp4Lecture)],
    )


class BaseCourse(BaseModel):
    id: str = Field(description="ID of the course")
    title: str = Field(description="Title of the course")
    description: str | None = Field(description="Description of the course")
    price: int = Field(min=0, description="Price of the course in morphcoins")

    Config = example(id="python", title="Python", description="Course description")

    @property
    def free(self) -> bool:
        return self.price == 0


class Course(BaseCourse):
    sections: list[Section] = Field(description="Sections in the course")

    Config = example(**get_example(BaseCourse), sections=[get_example(Section)])

    @property
    def summary(self) -> CourseSummary:
        return CourseSummary(
            **{key: value for key, value in self.dict().items() if key in BaseCourse.__fields__},
            sections=len(self.sections),
            lectures=sum(len(section.lectures) for section in self.sections),
        )


class CourseSummary(BaseCourse):
    sections: int = Field(description="Number of sections in the course")
    lectures: int = Field(description="Number of lectures in the course")

    Config = example(**get_example(BaseCourse), sections=42, lectures=1337)
