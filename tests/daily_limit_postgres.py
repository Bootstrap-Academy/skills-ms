"""Native migration/race checks against an already-started disposable PostgreSQL.

DATABASE_URL must point to an empty local database created only for this run.
Run: nix develop --command python tests/daily_limit_postgres.py
"""

import asyncio
import json
import os
from uuid import UUID, uuid4

from alembic import command
from alembic.config import Config

from sqlalchemy import text

from api import models
from api.database import db, db_context, filter_by
from api.schemas.course import Course
from api.schemas.daily_limit import LearningPolicy, LimitConfiguration
from api.schemas.user import User
from api.services import courses, daily_limit
from api.settings import settings


async def main() -> None:
    assert os.environ["DATABASE_URL"].startswith("postgresql+asyncpg://")
    settings.daily_limit_policy_enabled = True
    course = Course.parse_obj(
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
    async with db_context():
        assert (await db.exec(text("select version_num from skills_alembic_version"))).scalar() == "dailystarts001"
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
                "migration": "dailystarts001",
                "last_slot": outcomes,
                "concurrent_retries": len(retries),
                "rollback": "passed",
            }
        )
    )


if __name__ == "__main__":
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(main())
