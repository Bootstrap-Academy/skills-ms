from typing import Any

from fastapi.encoders import jsonable_encoder
from pydantic import Field, field_serializer

from api.schemas import BaseModel, Timestamp


class CourseAccess(BaseModel):
    course_id: str = Field(description="ID of the course the user has unlocked")


class LastWatch(BaseModel):
    course_id: str = Field(description="ID of the course")
    timestamp: Timestamp = Field(description="Point in time at which the user last watched this course")


class LectureProgress(BaseModel):
    course_id: str = Field(description="ID of the course")
    lecture_id: str = Field(description="ID of the lecture")
    completed: Timestamp = Field(description="Point in time at which the user completed this lecture")


class SubSkillBookmark(BaseModel):
    root_skill_id: str = Field(description="ID of the root skill")
    sub_skill_id: str = Field(description="ID of the bookmarked sub skill")


class XP(BaseModel):
    skill_id: str = Field(description="ID of the sub skill")
    xp: int = Field(description="Amount of XP the user has collected in this skill")
    last_update: Timestamp | None = Field(
        ..., description="Point in time at which the XP were last updated; null if no timestamp was recorded"
    )


class UserDataExport(BaseModel):
    """Everything this service stores about a single user.

    All points in time are ISO 8601 timestamps in UTC.
    """

    lesson_starts: list[dict[str, Any]] = Field(default_factory=list)
    lesson_start_requests: list[dict[str, Any]] = Field(default_factory=list)
    purchases: list[dict[str, Any]] = Field(default_factory=list)
    room_states: list[dict[str, Any]] = Field(default_factory=list)
    room_requests: list[dict[str, Any]] = Field(default_factory=list)
    purchase_user: list[dict[str, Any]] = Field(default_factory=list)
    retained_course_rights: list[dict[str, Any]] = Field(default_factory=list)
    course_right_grants: list[dict[str, Any]] = Field(default_factory=list)
    xp_operations: list[dict[str, Any]] = Field(default_factory=list)
    course_access: list[CourseAccess] = Field(description="Courses the user has unlocked")
    last_watch: list[LastWatch] = Field(description="When the user last watched each course")
    lecture_progress: list[LectureProgress] = Field(description="Lectures the user has completed")
    sub_skill_bookmarks: list[SubSkillBookmark] = Field(description="Sub skills the user has bookmarked")
    xp: list[XP] = Field(description="XP the user has collected per sub skill")

    @field_serializer(
        "lesson_starts",
        "lesson_start_requests",
        "purchases",
        "room_states",
        "room_requests",
        "purchase_user",
        "retained_course_rights",
        "course_right_grants",
        "xp_operations",
        when_used="json",
    )
    def serialize_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return jsonable_encoder(rows)  # type: ignore[no-any-return]
