"""Small shared learning-room contract, independent of legacy course rewards."""

import json
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, Field, root_validator, validator

from api.schemas.lesson_module import MODULE_ID_PATTERN, LessonModuleDescriptor


# Same shape as llm-ms profile IDs (`academy_llm/src/profiles.rs`, `valid_id`).
LLM_PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"
MAX_LLM_PROFILES = 16
# Same default cap as challenges-ms (`challenges.lesson_milestones.max_xp`).
MAX_MILESTONE_XP = 50


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
        "custom",
        "video",
    ]
    content: dict[str, Any]
    teaches: list[str]
    practices: list[str]
    requires: list[str]
    exercise: Exercise | None = None
    module: LessonModuleDescriptor | None = None


class IntroductionCompletion(RoomModel):
    kind: Literal["introduced"]
    answer: dict[str, Any]
    allow_skip: bool = False


class LlmVerdictCompletion(RoomModel):
    """Completion by a passing grading verdict that llm-ms signed for exactly this learner, unit and answer.

    `profile_sha256` pins the grading profile version (SHA-256 of its file, as llm-ms hashes it), so a
    verdict from a changed rubric does not count. The grader can be wrong, so skipping always stays open.
    """

    kind: Literal["llm-verdict"]
    profile: str = Field(regex=LLM_PROFILE_ID_PATTERN)
    profile_sha256: str = Field(regex=r"^[0-9a-f]{64}$")
    allow_skip: Literal[True] = True


class LessonMilestone(RoomModel):
    """XP that challenges-ms books once per learner when this unit is first completed by its server check.

    XP stay on the sub-skill node (`skills_xp`); challenges-ms deduplicates per learner and unit and caps the
    amount (`challenges.lesson_milestones.max_xp`, default 50).
    """

    skill_id: str = Field(regex=r"^[a-z0-9][a-z0-9_-]{0,255}$")
    xp: int = Field(ge=1, le=MAX_MILESTONE_XP)

    @validator("xp", pre=True)
    @classmethod
    def strict_xp(cls, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("Milestone XP must be an integer")
        return value


class CatalogueUnit(Unit):
    retired: bool
    completion: IntroductionCompletion | LlmVerdictCompletion | None = None
    module_id: str | None = Field(default=None, regex=MODULE_ID_PATTERN)
    # Server-side only: the llm-ms profiles a lesson grant for this unit covers.
    llm_profiles: list[str] = Field(default_factory=list, max_items=MAX_LLM_PROFILES)
    # Server-side only: lesson milestone XP (XP-02), booked in challenges-ms after a checked completion.
    milestone: LessonMilestone | None = None

    @validator("llm_profiles", each_item=True)
    @classmethod
    def llm_profile_id(cls, value: str) -> str:
        if not re.fullmatch(LLM_PROFILE_ID_PATTERN, value):
            raise ValueError("Invalid LLM profile ID")
        return value

    @root_validator(skip_on_failure=True)
    @classmethod
    def completion_matches_room(cls, values: dict[str, Any]) -> dict[str, Any]:
        if values.get("module") is not None:
            raise ValueError("Module URLs come from the operator registry, not content")
        profiles = values["llm_profiles"]
        if len(set(profiles)) != len(profiles):
            raise ValueError("Duplicate LLM profile")
        if isinstance(values.get("completion"), LlmVerdictCompletion):
            if values["room"] in ("exercise", "video"):
                raise ValueError("A graded answer belongs to a lesson room")
            if values["completion"].profile not in profiles:
                raise ValueError("The grading profile must be one of the unit's LLM profiles")
        if values.get("milestone") is not None and (
            values.get("completion") is None or values["room"] in ("exercise", "video")
        ):
            # Exercises earn XP through their subtask; a video records viewing, not mastery.
            raise ValueError("A lesson milestone needs a lesson with a server-side completion check")
        if values["room"] != "custom" and values.get("module_id") is not None:
            raise ValueError("Only a custom room references a module")
        if values["room"] in ("custom", "video"):
            if values.get("completion") is not None and values.get("exercise") is not None:
                raise ValueError("An activity has one server-side completion authority")
            if values["room"] == "custom" and values.get("module_id") is None:
                raise ValueError("A custom activity requires a registered module ID")
            if values["room"] == "video":
                for language in ("de", "en"):
                    localized = values["content"].get(language)
                    video = localized.get("video") if isinstance(localized, dict) else None
                    if not isinstance(video, dict):
                        raise ValueError("A video requires a supported source in each language")
                    if video.get("type") == "youtube":
                        video_id = video.get("id")
                        if not isinstance(video_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
                            raise ValueError("A video requires a valid YouTube ID")
                    elif video.get("type") == "mp4":
                        url = video.get("url")
                        if not isinstance(url, str):
                            raise ValueError("A video requires an absolute HTTPS media URL")
                        parsed = urlsplit(url)
                        if (
                            parsed.scheme != "https"
                            or not parsed.hostname
                            or parsed.username is not None
                            or parsed.password is not None
                            or "\\" in url
                            or any(ord(char) <= 32 for char in url)
                        ):
                            raise ValueError("A video requires an absolute HTTPS media URL")
                    else:
                        raise ValueError("A video requires a supported source in each language")
                completion = values.get("completion")
                if completion is not None and json.dumps(completion.answer, sort_keys=True) != '{"viewed": true}':
                    raise ValueError("A video introduction records viewed, not mastery")
            return values
        if (values["room"] != "exercise") != (values.get("completion") is not None):
            raise ValueError("Introductions require an internal completion check")
        if values["room"] != "exercise" and values.get("exercise") is not None:
            raise ValueError("Exercise references require the exercise room")
        return values

    def public(self) -> Unit:
        return Unit.parse_obj(self.dict(exclude={"retired", "completion", "module_id", "llm_profiles", "milestone"}))


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
    # The signed grading verdict from llm-ms (`outputs[0].grading.receipt`) for `answer.text`.
    verdict: str | None = Field(
        default=None, max_length=8192, regex=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
    )
    _bounded_answer = validator("answer", pre=True, allow_reuse=True)(bounded_object)


class StartReview(Mutation):
    pass


class LlmGrant(RoomModel):
    """A short-lived lesson grant for llm-ms; the host sends it with every call for this unit."""

    grant: str
    profiles: list[str]
    expires_at: datetime
