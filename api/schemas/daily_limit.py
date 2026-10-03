"""Shared learning access wire contract."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, StrictBool, StrictStr


class LearningPolicy(BaseModel):
    mode: Literal["legacy", "shadow", "daily"]
    premium: StrictBool
    single_course_sales: StrictBool
    heart_sales: StrictBool


class DailyStatus(BaseModel):
    mode: Literal["legacy", "shadow", "daily"]
    limit: int
    used: int
    remaining: int | None
    resets_at: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    unlimited: bool = False
    enforced: bool = False
    started: bool = False
    can_start: bool = True
    exempt: Literal["premium", "admin", "purchase", "started"] | None = None


class StartLesson(BaseModel):
    request_id: UUID


class StartResult(BaseModel):
    started: bool
    daily: DailyStatus | None


class LimitConfiguration(BaseModel):
    mode: Literal["off", "shadow", "enforce"]
    limit: int = Field(3, ge=1, le=100)
    updated_by: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=512)


class LectureBinding(BaseModel):
    course_id: str = Field(min_length=1, max_length=256)
    lecture_id: str | None = Field(None, min_length=1, max_length=256)
    section_id: str | None = Field(None, min_length=1, max_length=256)


class ChallengeAdmission(BaseModel):
    task_id: UUID | None = None
    subtask_id: UUID | None = None
    lecture_bindings: list[LectureBinding] = Field(default_factory=list, max_items=100)
    user_admin: StrictBool = False
    request_id: UUID | None = None


class ChallengeRead(ChallengeAdmission):
    """A concrete, read-only decision; a batch cannot start lessons."""

    lecture_bindings: list[LectureBinding] = Field(default_factory=list, max_items=1)
    request_id: None = None


class ChallengeReadBatch(BaseModel):
    requests: list[ChallengeRead] = Field(min_items=1, max_items=250)


class HistoryLecture(BaseModel):
    course_id: StrictStr
    lecture_id: StrictStr


class LearningHistory(BaseModel):
    attempted_subtask_ids: list[UUID]
    attempted_lecture_bindings: list[HistoryLecture]
