"""Ordered lesson activities with explicit adapters to the existing authorities."""

from typing import Any, Literal

from pydantic import Field, root_validator

from api.schemas.lesson_module import LessonModuleDescriptor
from api.schemas.rooms import Exercise, LocalizedText, Progress, RoomModel


class RoomSource(RoomModel):
    kind: Literal["room"] = "room"
    unit_id: str


class LectureSource(RoomModel):
    kind: Literal["lecture"] = "lecture"
    course_id: str
    section_id: str
    lecture_id: str


class ChallengeSource(Exercise):
    kind: Literal["challenge"] = "challenge"


ActivitySource = RoomSource | LectureSource | ChallengeSource
ActivityRole = Literal["explanation", "practice"]
ActivityKind = Literal["video", "explainer", "quiz", "matching", "coding", "custom", "legacy-room"]


class ActivityReference(RoomModel):
    id: str = Field(min_length=1, max_length=256)
    source: RoomSource | LectureSource = Field(discriminator="kind")
    title: LocalizedText | None = None
    roles: list[ActivityRole] | None = Field(default=None, min_items=1)

    @root_validator(pre=True)
    @classmethod
    def require_persistent_assessment(cls, values: dict[str, Any]) -> dict[str, Any]:
        source = values.get("source")
        if isinstance(source, dict) and source.get("kind") == "challenge":
            raise ValueError(
                "Authored assessments must reference a room with an ExerciseRef; direct challenges are legacy adapters"
            )
        return values

    @root_validator(skip_on_failure=True)
    @classmethod
    def stable_identity(cls, values: dict[str, Any]) -> dict[str, Any]:
        source = values["source"]
        if isinstance(source, RoomSource) and values["id"] != source.unit_id:
            raise ValueError("A room activity keeps its existing unit ID")
        return values


class LessonDefinition(RoomModel):
    id: str = Field(min_length=1, max_length=256)
    title: LocalizedText
    chapter_id: str | None = None
    activities: list[ActivityReference] = Field(min_items=1)

    @root_validator(skip_on_failure=True)
    @classmethod
    def unique_activities(cls, values: dict[str, Any]) -> dict[str, Any]:
        ids = [activity.id for activity in values["activities"]]
        if len(ids) != len(set(ids)):
            raise ValueError("A lesson cannot contain the same activity twice")
        return values


class CurriculumChapter(RoomModel):
    id: str = Field(min_length=1, max_length=256)
    title: LocalizedText


class CurriculumDefinition(RoomModel):
    chapters: list[CurriculumChapter] = Field(default_factory=list)
    lessons: list[LessonDefinition] = Field(min_items=1)

    @root_validator(skip_on_failure=True)
    @classmethod
    def valid_structure(cls, values: dict[str, Any]) -> dict[str, Any]:
        chapters, lessons = values["chapters"], values["lessons"]
        chapter_ids = {chapter.id for chapter in chapters}
        if len(chapter_ids) != len(chapters) or len({lesson.id for lesson in lessons}) != len(lessons):
            raise ValueError("Duplicate chapter or lesson IDs")
        identities: dict[str, ActivitySource] = {}
        for lesson in lessons:
            if lesson.chapter_id is not None and lesson.chapter_id not in chapter_ids:
                raise ValueError("Unknown lesson chapter")
            for activity in lesson.activities:
                if activity.id in identities and identities[activity.id] != activity.source:
                    raise ValueError("One activity ID cannot refer to different work")
                identities[activity.id] = activity.source
        return values


class LessonSummary(RoomModel):
    id: str
    title: LocalizedText
    chapter_id: str | None = None
    activity_ids: list[str]
    completed: bool


class Curriculum(RoomModel):
    course_id: str
    explicit: bool
    chapters: list[CurriculumChapter]
    lessons: list[LessonSummary]


class Activity(RoomModel):
    id: str
    kind: ActivityKind
    roles: list[ActivityRole]
    title: LocalizedText
    source: ActivitySource = Field(discriminator="kind")
    content: dict[str, Any]
    room: str | None = None
    exercise: Exercise | None = None
    module: LessonModuleDescriptor | None = None
    progress: Progress | None = None
    completed: bool | None = None
    skip_allowed: bool = False


class LegacyPractice(RoomModel):
    course_id: str
    section_id: str
    lecture_id: str


class Lesson(RoomModel):
    course_id: str
    explicit: bool
    id: str
    title: LocalizedText
    chapter_id: str | None = None
    activities: list[Activity]
    completed: bool
    legacy_practice: LegacyPractice | None = None
