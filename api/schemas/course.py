from __future__ import annotations

from pydantic import BaseModel, Field

from api.utils.docs import example, get_example


class YoutubeLecture(BaseModel):
    id: str = Field(description="ID of the lecture")
    title: str = Field(description="Title of the lecture")
    description: str | None = Field(description="Description of the lecture")
    type = Field("youtube", const=True, description="Type of the lecture")
    video_id: str = Field(description="Youtube Video ID of the lecture")
    duration: int = Field(description="Duration of the lecture in seconds")

    Config = example(
        id="intro",
        title="Introduction",
        description="Introduction to the course",
        type="youtube",
        video_id="dQw4w9WgXcQ",
        duration=100,
    )

    def to_user_lecture(self, completed: bool) -> UserLecture:
        return UserYoutubeLecture(**self.dict(), completed=completed)


class Mp4Lecture(BaseModel):
    id: str = Field(description="ID of the lecture")
    title: str = Field(description="Title of the lecture")
    description: str | None = Field(description="Description of the lecture")
    type = Field("mp4", const=True, description="Type of the lecture")
    duration: int = Field(description="Duration of the lecture in seconds")

    Config = example(
        id="intro", title="Introduction", description="Introduction to the course", type="mp4", duration=100
    )

    def to_user_lecture(self, completed: bool) -> UserLecture:
        return UserMp4Lecture(**self.dict(), completed=completed)


Lecture = YoutubeLecture | Mp4Lecture


class LectureSummary(BaseModel):
    title: str = Field(description="Title of the lecture")
    duration: int = Field(description="Duration of the lecture in seconds")
    completed: bool | None = Field(description="If the lecture is completed")


class Section(BaseModel):
    id: str = Field(description="ID of the section")
    title: str = Field(description="Title of the section")
    description: str | None = Field(description="Description of the section")
    lectures: list[Lecture] = Field(description="Lectures in the section")

    Config = example(
        id="intro",
        title="Introduction",
        description="Introduction to the course",
        lectures=[get_example(YoutubeLecture), get_example(Mp4Lecture)],
    )


class SectionSummary(BaseModel):
    title: str = Field(description="Title of the section")
    lectures: list[LectureSummary] = Field(description="Lectures in the section")
    completed: bool | None = Field(description="If the section is completed")


class BaseCourse(BaseModel):
    id: str = Field(description="ID of the course")
    title: str = Field(description="Title of the course")
    description: str | None = Field(description="Description of the course")
    category: str | None = Field(description="Category of the course")
    language: str | None = Field(description="Language of the course")
    image: str | None = Field(description="Image URL of the course")
    authors: list[dict[str, str]] = Field(description="Authors of the course")
    price: int = Field(min=0, description="Price of the course in morphcoins")
    learning_goals: list[str] = Field(description="Learning goals of the course")
    requirements: list[str] = Field(description="Requirements of the course")
    last_update: int = Field(description="Timestamp of last update of the course")

    Config = example(
        id="python",
        title="Python",
        description="Course description",
        category="Programming",
        language="en",
        image="https://example.com/image.png",
        authors=[{"name": "John Doe", "url": "https://example.com"}],
        price=100,
        learning_goals=["Learn Python"],
        requirements=["Basic knowledge of programming"],
        last_update=1620000000,
    )

    @property
    def free(self) -> bool:
        return self.price == 0


class Course(BaseCourse):
    sections: list[Section] = Field(description="Sections in the course")

    Config = example(**get_example(BaseCourse), sections=[get_example(Section)])

    def summary(self, completed_lectures: set[str] | None) -> CourseSummary:
        sections = []
        for section in self.sections:
            lectures = [
                LectureSummary(
                    title=lecture.title,
                    duration=lecture.duration,
                    completed=None if completed_lectures is None else lecture.id in completed_lectures,
                )
                for lecture in section.lectures
            ]
            sections.append(
                SectionSummary(
                    title=section.title,
                    lectures=lectures,
                    completed=None if completed_lectures is None else all(lecture.completed for lecture in lectures),
                )
            )

        return CourseSummary(
            **{key: value for key, value in self.dict().items() if key in BaseCourse.__fields__},
            sections=sections,
            completed=None if completed_lectures is None else all(section.completed for section in sections),
        )

    def to_user_course(self, completed_lectures: set[str]) -> UserCourse:
        return UserCourse(
            **{
                **self.dict(),
                "sections": [
                    UserSection(
                        **{
                            **section.dict(),
                            "lectures": [
                                lecture.to_user_lecture(lecture.id in completed_lectures)
                                for lecture in section.lectures
                            ],
                        }
                    )
                    for section in self.sections
                ],
            }
        )


class CourseSummary(BaseCourse):
    sections: list[SectionSummary] = Field(description="Lectures of the course")
    completed: bool | None = Field(description="If the course is completed")


class UserYoutubeLecture(YoutubeLecture):
    completed: bool = Field(description="If the lecture is completed")

    Config = example(**get_example(YoutubeLecture), completed=True)


class UserMp4Lecture(Mp4Lecture):
    completed: bool = Field(description="If the lecture is completed")

    Config = example(**get_example(Mp4Lecture), completed=True)


UserLecture = UserYoutubeLecture | UserMp4Lecture


class UserSection(Section):
    lectures: list[UserLecture] = Field(description="Lectures in the section")  # type: ignore

    Config = example(
        **{**get_example(Section), "lectures": [get_example(UserYoutubeLecture), get_example(UserMp4Lecture)]}
    )


class UserCourse(Course):
    sections: list[UserSection] = Field(description="Sections in the course")  # type: ignore

    Config = example(**{**get_example(Course), "sections": [get_example(UserSection)]})


class NextUnseenResponse(BaseModel):
    lecture: Lecture
    section: Section
