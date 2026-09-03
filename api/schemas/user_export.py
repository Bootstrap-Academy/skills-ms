from datetime import datetime

from pydantic import BaseModel, Field


class CourseAccess(BaseModel):
    course_id: str = Field(description="ID of the course the user has unlocked")


class LastWatch(BaseModel):
    course_id: str = Field(description="ID of the course")
    timestamp: datetime = Field(description="Point in time at which the user last watched this course")


class LectureProgress(BaseModel):
    course_id: str = Field(description="ID of the course")
    lecture_id: str = Field(description="ID of the lecture")
    completed: datetime = Field(description="Point in time at which the user completed this lecture")


class SubSkillBookmark(BaseModel):
    root_skill_id: str = Field(description="ID of the root skill")
    sub_skill_id: str = Field(description="ID of the bookmarked sub skill")


class XP(BaseModel):
    skill_id: str = Field(description="ID of the sub skill")
    xp: int = Field(description="Amount of XP the user has collected in this skill")
    last_update: datetime = Field(description="Point in time at which the XP were last updated")


class UserDataExport(BaseModel):
    """Everything this service stores about a single user.

    All points in time are ISO 8601 timestamps in UTC.
    """

    course_access: list[CourseAccess] = Field(description="Courses the user has unlocked")
    last_watch: list[LastWatch] = Field(description="When the user last watched each course")
    lecture_progress: list[LectureProgress] = Field(description="Lectures the user has completed")
    sub_skill_bookmarks: list[SubSkillBookmark] = Field(description="Sub skills the user has bookmarked")
    xp: list[XP] = Field(description="XP the user has collected per sub skill")
