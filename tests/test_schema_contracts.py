"""Contract regressions across the native Pydantic migration."""

from datetime import datetime, timezone

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from api.schemas import BaseModel
from api.schemas.course import Course
from api.schemas.daily_limit import DailyStatus, LearningPolicy
from api.schemas.skill import UpdateRootSkill, UpdateRootTree, UpdateSubSkill
from api.schemas.user import UserAccessToken
from api.schemas.user_export import XP, LastWatch, UserDataExport
from api.schemas.xp import UpdateXP
from api.utils.docs import get_example


@pytest.mark.parametrize("schema", [UpdateRootTree, UpdateRootSkill, UpdateSubSkill, UpdateXP])
def test__partial_updates_keep_omitted_fields(schema: type[BaseModel]) -> None:
    update = schema()
    assert update.model_dump(exclude_unset=True) == {}
    assert all(value is None for value in update.model_dump().values())


def test__courses_keep_optional_metadata_and_lecture_defaults() -> None:
    course = Course.model_validate(
        {
            "id": "minimal",
            "title": "Minimal",
            "authors": [],
            "price": 0,
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            "sections": [
                {
                    "id": "intro",
                    "title": "Introduction",
                    "lectures": [{"id": "intro", "title": "Introduction", "video_id": "example", "duration": 1}],
                }
            ],
        }
    )
    assert (course.description, course.category, course.language, course.image) == (None, None, None, None)
    section = course.sections[0]
    assert section.description is None and section.lectures[0].description is None
    assert course.to_user_course(set()).model_dump(mode="json")["sections"][0]["lectures"][0] == {
        "id": "intro",
        "title": "Introduction",
        "description": None,
        "type": "youtube",
        "video_id": "example",
        "duration": 1,
        "completed": False,
    }
    assert Course.model_json_schema()["example"] == get_example(Course)


def test__ordinary_token_extensions_are_ignored_and_learning_policy_is_strict() -> None:
    token = UserAccessToken.model_validate(
        {
            "uid": "user",
            "rt": "session",
            "exp": 123456,
            "data": {"email_verified": True, "admin": False, "display_name": "Name"},
        }
    )
    assert token.to_user().model_dump() == {"id": "user", "email_verified": True, "admin": False}
    policy = {"mode": "daily", "premium": False, "single_course_sales": False, "heart_sales": False}
    with pytest.raises(ValidationError):
        LearningPolicy.model_validate({**policy, "unknown": True})
    with pytest.raises(ValidationError):
        LearningPolicy.model_validate({**policy, "premium": "false"})


def test__exports_and_daily_status_keep_isoformat_timestamps() -> None:
    timestamp = datetime(2026, 9, 11, 12, 34, 56, 123456, tzinfo=timezone.utc)
    export = UserDataExport(
        course_access=[],
        last_watch=[LastWatch(course_id="course", timestamp=timestamp)],
        lecture_progress=[],
        sub_skill_bookmarks=[],
        xp=[XP(skill_id="skill", xp=7, last_update=timestamp), XP(skill_id="old", xp=42, last_update=None)],
        room_states=[{"updated_at": timestamp, "state": {"text": "2026-09-11T12:34:56Z"}}],
    )
    encoded = jsonable_encoder(export)
    assert encoded["last_watch"][0]["timestamp"] == timestamp.isoformat()
    assert encoded["xp"][0]["last_update"] == timestamp.isoformat()
    assert encoded["xp"][1]["last_update"] is None
    assert encoded["room_states"][0] == {"updated_at": timestamp.isoformat(), "state": {"text": "2026-09-11T12:34:56Z"}}
    daily = DailyStatus(mode="daily", limit=3, used=1, resets_at=timestamp)
    assert daily.remaining is None and jsonable_encoder(daily)["resets_at"] == timestamp.isoformat()
    assert DailyStatus.model_json_schema(mode="serialization")["properties"]["resets_at"]["format"] == "date-time"
    with pytest.raises(ValidationError):
        XP.model_validate({"skill_id": "missing", "xp": 0})
