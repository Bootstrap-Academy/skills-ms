from datetime import datetime, timedelta, timezone

import pytest
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context
from api.endpoints.course import _resolve_current_course, _resolve_next_course
from api.schemas.course import Course, Section, YoutubeLecture


def _build_course(course_id: str, lecture_ids: list[str]) -> Course:
    return Course(
        id=course_id,
        title=f"{course_id} title",
        description=None,
        category=None,
        language="en",
        image=None,
        authors=[{"name": "Author", "url": "https://example.com"}],
        price=0,
        learning_goals=[],
        requirements=[],
        last_update=0,
        sections=[
            Section(
                id=f"{course_id}-section",
                title="Section",
                description=None,
                lectures=[
                    YoutubeLecture(
                        id=lecture_id,
                        title=f"Lecture {lecture_id}",
                        description=None,
                        video_id=f"{lecture_id}-vid",
                        duration=60,
                    )
                    for lecture_id in lecture_ids
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_resolve_current_course_prefers_latest_activity(mocker: MockerFixture) -> None:
    user_id = "user-1"
    now = datetime.now(timezone.utc)
    courses = {
        "course-1": _build_course("course-1", ["c1-1", "c1-2"]),
        "course-2": _build_course("course-2", ["c2-1", "c2-2"]),
    }
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=now,
            )
        )
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-2",
                lecture_id="c2-1",
                completed=now - timedelta(days=1),
            )
        )
        result = await _resolve_current_course(user_id)

    assert result is not None
    course, completed_lectures = result
    assert course.id == "course-1"
    assert completed_lectures == {"c1-1"}


@pytest.mark.asyncio
async def test_resolve_current_course_returns_none_without_progress(mocker: MockerFixture) -> None:
    user_id = "user-2"
    courses = {"course-3": _build_course("course-3", ["c3-1"])}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        result = await _resolve_current_course(user_id)

    assert result is None


@pytest.mark.asyncio
async def test_resolve_next_course_prefers_bookmarks(mocker: MockerFixture) -> None:
    user_id = "user-3"
    courses = {
        "course-a": _build_course("course-a", ["ca-1", "ca-2"]),
        "course-b": _build_course("course-b", ["cb-1"]),
    }
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)
    mocker.patch("api.endpoints.course.choice", side_effect=lambda seq: seq[0])

    async with db_context():
        await db.add(
            models.RootSkill(
                id="root-1",
                name="Root",
                row=0,
                column=0,
                sub_tree_rows=1,
                sub_tree_columns=1,
                icon=None,
            )
        )
        await db.add(
            models.SubSkill(
                id="skill-1",
                parent_id="root-1",
                name="Skill",
                row=0,
                column=0,
                icon=None,
            )
        )
        await db.add(models.SkillCourse(skill_id="skill-1", course_id="course-a"))
        await db.add(
            models.SubSkillBookmark(
                user_id=user_id,
                root_skill_id="root-1",
                sub_skill_id="skill-1",
            )
        )
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-a",
                lecture_id="ca-1",
                completed=datetime.now(timezone.utc),
            )
        )
        next_course = await _resolve_next_course(user_id)

    assert next_course is not None
    course, completed = next_course
    assert course.id == "course-a"
    assert completed == {"ca-1"}


@pytest.mark.asyncio
async def test_resolve_next_course_skips_completed_courses(mocker: MockerFixture) -> None:
    user_id = "user-4"
    courses = {
        "course-a": _build_course("course-a", ["ca-1", "ca-2"]),
        "course-b": _build_course("course-b", ["cb-1"]),
    }
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)
    mocker.patch("api.endpoints.course.choice", side_effect=lambda seq: seq[0])

    async with db_context():
        await db.add(
            models.RootSkill(
                id="root-2",
                name="Root",
                row=0,
                column=0,
                sub_tree_rows=1,
                sub_tree_columns=1,
                icon=None,
            )
        )
        await db.add(
            models.SubSkill(
                id="skill-2",
                parent_id="root-2",
                name="Skill",
                row=0,
                column=0,
                icon=None,
            )
        )
        await db.add(models.SkillCourse(skill_id="skill-2", course_id="course-a"))
        await db.add(models.SkillCourse(skill_id="skill-2", course_id="course-b"))
        await db.add(
            models.SubSkillBookmark(
                user_id=user_id,
                root_skill_id="root-2",
                sub_skill_id="skill-2",
            )
        )
        # mark course-a as fully completed
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-a",
                lecture_id="ca-1",
                completed=datetime.now(timezone.utc),
            )
        )
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-a",
                lecture_id="ca-2",
                completed=datetime.now(timezone.utc),
            )
        )
        next_course = await _resolve_next_course(user_id)

    assert next_course is not None
    course, completed = next_course
    assert course.id == "course-b"
    assert completed == set()
