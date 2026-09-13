"""Small shared learning-room contract, independent of legacy course rewards."""

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, root_validator, validator


class RoomModel(BaseModel):
    class Config:
        extra = "forbid"


class LocalizedText(RoomModel):
    de: str
    en: str


class Exercise(RoomModel):
    type: Literal["multiple_choice", "matching", "coding"]
    task_id: UUID
    subtask_id: UUID


class Unit(RoomModel):
    id: str = Field(regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    path_id: str = Field(regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    chapter_id: str | None = Field(default=None, regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: LocalizedText
    room: Literal[
        "loop-explorer",
        "percentage-explorer",
        "guided-lesson",
        "io-machine",
        "bit-lab",
        "file-workspace",
        "step-machine",
        "network-lab",
        "exercise",
    ]
    content: dict[str, Any]
    teaches: list[str]
    practices: list[str]
    requires: list[str]
    exercise: Exercise | None = None


class IntroductionCompletion(RoomModel):
    kind: Literal["introduced"]
    answer: dict[str, Any]
    allow_skip: bool = False


class CatalogueUnit(Unit):
    retired: bool
    completion: IntroductionCompletion | None = None

    @root_validator(skip_on_failure=True)
    @classmethod
    def completion_matches_room(cls, values: dict[str, Any]) -> dict[str, Any]:
        if (values["room"] != "exercise") != (values.get("completion") is not None):
            raise ValueError("Introductions require an internal completion check")
        if values["room"] != "exercise" and values.get("exercise") is not None:
            raise ValueError("Exercise references require the exercise room")
        return values

    def public(self) -> Unit:
        return Unit.parse_obj(self.dict(exclude={"retired", "completion"}))


class LearningChapter(RoomModel):
    id: str = Field(regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: LocalizedText


class LearningPath(RoomModel):
    id: str = Field(regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: LocalizedText
    direction_id: str | None = Field(default=None, regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    chapters: list[LearningChapter] = Field(default_factory=list)


class CataloguePath(LearningPath):
    units: list[str]


class Catalogue(RoomModel):
    paths: list[CataloguePath]
    units: list[CatalogueUnit]

    @root_validator(skip_on_failure=True)
    @classmethod
    def references_are_consistent(cls, values: dict[str, Any]) -> dict[str, Any]:
        paths, units = values["paths"], values["units"]
        by_id = {unit.id: unit for unit in units}
        if len(by_id) != len(units) or len({path.id for path in paths}) != len(paths):
            raise ValueError("Duplicate learning-room identifiers")
        seen: set[str] = set()
        for path in paths:
            chapter_positions = {chapter.id: index for index, chapter in enumerate(path.chapters)}
            if len(chapter_positions) != len(path.chapters):
                raise ValueError("Duplicate learning-chapter identifiers in a path")
            previous_chapter = -1
            for unit_id in path.units:
                if unit_id in seen or unit_id not in by_id or by_id[unit_id].path_id != path.id:
                    raise ValueError("Invalid learning-path reference")
                chapter_id = by_id[unit_id].chapter_id
                if chapter_positions and chapter_id is None:
                    raise ValueError("Every unit in a chaptered path must belong to a chapter")
                if chapter_id is not None:
                    if chapter_id not in chapter_positions:
                        raise ValueError("Invalid learning-chapter reference")
                    position = chapter_positions[chapter_id]
                    if position < previous_chapter:
                        raise ValueError("Learning units must follow contiguous chapters in declared order")
                    previous_chapter = position
                seen.add(unit_id)
        if seen != set(by_id):
            raise ValueError("Every unit must belong to exactly one path")
        return values


class Result(RoomModel):
    kind: Literal["introduced", "solved"]


class Progress(RoomModel):
    revision: int = 0
    state: dict[str, Any] = Field(default_factory=dict)
    status: Literal["new", "in_progress", "completed", "skipped"] = "new"
    result: Result | None = None
    review_id: UUID | None = None


class RoomEnvelope(RoomModel):
    unit: Unit
    progress: Progress
    review_available: bool = False


class Rooms(RoomModel):
    paths: list[LearningPath]
    path: LearningPath
    next: RoomEnvelope | None
    empty_reason: Literal["completed", "unavailable", "prerequisites"] | None = None


class CourseLearningUnit(RoomModel):
    id: str
    chapter_id: str | None = Field(default=None, regex=r"^[a-z0-9][a-z0-9-]{0,79}$")
    title: LocalizedText
    room: str
    status: Literal["new", "in_progress", "completed", "skipped"]
    result: Result | None
    available: bool
    selectable: bool = False


class CourseLearning(RoomModel):
    path: LearningPath
    units: list[CourseLearningUnit]
    next: RoomEnvelope | None
    completed: bool
    empty_reason: Literal["completed", "unavailable", "prerequisites"] | None = None


def bounded_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required")
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid JSON state") from None
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("Room state exceeds 64 KiB")
    return value


class Mutation(RoomModel):
    request_id: UUID
    expected_revision: int = Field(ge=0, le=2147483646)

    @validator("expected_revision", pre=True)
    @classmethod
    def strict_revision(cls, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("An integer revision is required")
        return value


class SaveState(Mutation):
    review_id: UUID | None = None
    state: dict[str, Any]
    _bounded_state = validator("state", pre=True, allow_reuse=True)(bounded_object)


class Complete(Mutation):
    review_id: UUID | None = None
    attempt_id: UUID | None = None
    action: Literal["complete", "skip"]
    answer: dict[str, Any] = Field(default_factory=dict)
    _bounded_answer = validator("answer", pre=True, allow_reuse=True)(bounded_object)


class StartReview(Mutation):
    pass
