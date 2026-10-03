"""Native migration/race checks against an already-started disposable PostgreSQL.

DATABASE_URL must point to an empty local database created only for this run.
Run: nix develop --command python tests/daily_limit_postgres.py
"""

import asyncio
import json
import os
from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config

from sqlalchemy import text

from api import models
from api.database import db, db_context, filter_by
from api.schemas.course import Course
from api.schemas.daily_limit import LearningHistory, LearningPolicy, LimitConfiguration
from api.schemas.user import User
from api.services import courses, daily_limit
from api.settings import settings


async def seed_pre_policy_starts() -> None:
    async with db_context():
        for reason in ("daily", "premium", "shadow", "off", "historical"):
            await db.exec(
                text(
                    "INSERT INTO skills_lesson_starts "
                    "(user_id, course_id, lesson_id, started_at, local_day, charged, reason) "
                    "VALUES (:user, 'old-course', :reason, :started, :day, :charged, :reason)"
                ).bindparams(
                    user="migration",
                    reason=reason,
                    started=datetime(2026, 9, 25, 12),
                    day=date(2026, 9, 25),
                    charged=reason == "daily",
                )
            )
    await db.dispose()


async def main() -> None:
    assert os.environ["DATABASE_URL"].startswith("postgresql+asyncpg://")
    settings.daily_limit_policy_enabled = True
    course = Course.model_validate(
        {
            "id": "native",
            "title": "Synthetic",
            "description": None,
            "category": None,
            "language": "en",
            "image": None,
            "authors": [],
            "price": 0,
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            "sections": [
                {
                    "id": "section",
                    "title": "Synthetic",
                    "description": None,
                    "lectures": [
                        {
                            "id": f"lecture-{i}",
                            "title": "Synthetic",
                            "description": None,
                            "type": "youtube",
                            "video_id": "synthetic",
                            "duration": 60,
                        }
                        for i in range(12)
                    ],
                }
            ],
        }
    )
    courses.COURSES = {course.id: course}

    async def policy(user_id: str) -> LearningPolicy:
        return LearningPolicy(mode="daily", premium=False, single_course_sales=False, heart_sales=False)

    daily_limit.policy = policy

    async def history(user_id: str, payload: dict[str, Any]) -> LearningHistory:
        return LearningHistory(attempted_subtask_ids=[], attempted_lecture_bindings=[])

    daily_limit.read_history_batch = history
    async with db_context():
        assert (await db.exec(text("select version_num from skills_alembic_version"))).scalar() == "dailypolicy001"
        old = await db.all(filter_by(models.LessonStart, user_id="migration"))
        assert len(old) == 5
        assert {row.reason: row.policy_mode for row in old} == {
            "daily": "daily",
            "premium": None,
            "shadow": None,
            "off": None,
            "historical": None,
        }
        assert all(
            row.course_id == "old-course"
            and row.lesson_id == row.reason
            and row.local_day == date(2026, 9, 25)
            and row.started_at.replace(tzinfo=None) == datetime(2026, 9, 25, 12)
            and row.charged == (row.reason == "daily")
            for row in old
        )
        assert await daily_limit.configuration() == ("off", 3)
        await daily_limit.configure(
            LimitConfiguration(mode="enforce", limit=3, updated_by="native-test", note="Disposable DB")
        )

    async def begin(uid: str, i: int, request: UUID | None = None) -> int:
        try:
            async with db_context():
                user = User(id=uid, admin=False, email_verified=True)
                await daily_limit.start(
                    user, course, daily_limit.lecture_lesson(course, f"lecture-{i}"), request or uuid4()
                )
            return 200
        except daily_limit.AccessError as exc:
            return exc.status_code

    # Seed two starts, race twenty independent transactions for the last slot.
    for i in (0, 1):
        assert await begin("race", i) == 200
    outcomes = await asyncio.gather(*(begin("race", i) for i in range(2, 12)))
    assert outcomes.count(200) == 1 and outcomes.count(429) == 9, outcomes
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id="race", charged=True)) == 3
    # Same request and lesson racing before the user guard even exists.
    request = uuid4()
    retries = await asyncio.gather(*(begin("retry", 0, request) for _ in range(10)))
    assert retries == [200] * 10, retries
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id="retry")) == 1
        assert await db.count(filter_by(models.LessonStartRequest, user_id="retry")) == 1
        row = await db.first(filter_by(models.LessonStart, user_id="retry"))
        assert row is not None and row.policy_mode == "daily"
    assert await begin("retry", 1, request) == 409
    # A transaction failing after admission does not consume a slot.
    try:
        async with db_context():
            user = User(id="rollback", admin=False, email_verified=True)
            await daily_limit.start(user, course, daily_limit.lecture_lesson(course, "lecture-0"), uuid4())
            raise RuntimeError("synthetic later write failure")
    except RuntimeError:
        pass
    async with db_context():
        assert await db.count(filter_by(models.LessonStart, user_id="rollback")) == 0
    await db.dispose()
    print(
        json.dumps(
            {
                "migration": "dailypolicy001",
                "existing_starts_preserved": 5,
                "only_proven_daily_migrated": True,
                "last_slot": outcomes,
                "concurrent_retries": len(retries),
                "rollback": "passed",
            }
        )
    )


if __name__ == "__main__":
    command.upgrade(Config("alembic.ini"), "dailystarts001")
    asyncio.run(seed_pre_policy_starts())
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(main())
