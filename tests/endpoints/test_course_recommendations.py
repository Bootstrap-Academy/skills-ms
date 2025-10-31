from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context
from api.database import challenges as challenges_db
from api.endpoints.course import (
    _get_completed_lecture_ids,
    _resolve_current_course,
    _resolve_next_course,
    _resolve_next_lab_recommendation,
    _resolve_next_lecture_recommendation,
    _resolve_next_task_recommendation,
)
from api.schemas.course import Course, Section, YoutubeLecture
from api.services.challenges import get_unsolved_labs_for_lecture, get_unsolved_quizzes_for_lecture


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


@pytest.mark.asyncio
async def test_resolve_next_lecture_recommendation_returns_next_unseen(mocker: MockerFixture) -> None:
    user_id = "user-lecture"
    now = datetime.now(timezone.utc)
    courses = {"course-1": _build_course("course-1", ["c1-1", "c1-2"])}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=now - timedelta(days=1),
            )
        )

    recommendation = await _resolve_next_lecture_recommendation(user_id)
    assert recommendation is not None
    assert recommendation.course.id == "course-1"
    assert recommendation.lecture.id == "c1-2"


async def _insert_challenge_records(
    *,
    course_id: str,
    section_id: str,
    lecture_id: str,
    subtask_id: str,
    task_id: str,
    solved_timestamp: datetime | None,
    user_id: str,
    subtask_type: str = "multiple_choice_question",
) -> None:
    await challenges_db.execute(
        challenges_db.challenges_course_tasks.insert().values(
            task_id=task_id,
            course_id=course_id,
            section_id=section_id,
            lecture_id=lecture_id,
        )
    )
    await challenges_db.execute(
        challenges_db.challenges_subtasks.insert().values(
            id=subtask_id,
            task_id=task_id,
            creator=str(uuid4()),
            creation_timestamp=datetime.now(timezone.utc),
            xp=5,
            coins=0,
            enabled=True,
            ty=subtask_type,
            retired=False,
        )
    )
    if solved_timestamp is not None:
        await challenges_db.execute(
            challenges_db.challenges_user_subtasks.insert().values(
                user_id=user_id,
                subtask_id=subtask_id,
                solved_timestamp=solved_timestamp,
                rating=None,
                rating_timestamp=None,
                last_attempt_timestamp=solved_timestamp,
                attempts=1,
            )
        )


@pytest.mark.asyncio
async def test_resolve_next_task_prefers_latest_unsolved(mocker: MockerFixture) -> None:
    user_id = "user-tasks"
    course = _build_course("course-1", ["c1-1", "c1-2"])
    courses = {"course-1": course}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-2",
                completed=datetime(2024, 1, 2, tzinfo=timezone.utc),
            )
        )

    completed_ids = await _get_completed_lecture_ids(user_id, "course-1")
    assert completed_ids == ["c1-2", "c1-1"]

    section_id = course.sections[0].id
    solved_task = str(uuid4())
    solved_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-1",
        subtask_id=solved_subtask,
        task_id=solved_task,
        solved_timestamp=datetime.now(timezone.utc),
        user_id=user_id,
    )

    unsolved_task = str(uuid4())
    unsolved_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-2",
        subtask_id=unsolved_subtask,
        task_id=unsolved_task,
        solved_timestamp=None,
        user_id=user_id,
    )

    quizzes = await get_unsolved_quizzes_for_lecture(
        user_id=user_id,
        course_id="course-1",
        lecture_id="c1-2",
    )
    assert [quiz.subtask_id for quiz in quizzes] == [unsolved_subtask]

    recommendation = await _resolve_next_task_recommendation(user_id)
    assert recommendation is not None
    assert recommendation.lecture.id == "c1-2"
    assert recommendation.task.subtask_id == unsolved_subtask


@pytest.mark.asyncio
async def test_resolve_next_task_falls_back_to_unseen_lecture(mocker: MockerFixture) -> None:
    user_id = "user-fallback"
    course = _build_course("course-1", ["c1-1", "c1-2"])
    courses = {"course-1": course}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
        )

    section_id = course.sections[0].id
    solved_task = str(uuid4())
    solved_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-1",
        subtask_id=solved_subtask,
        task_id=solved_task,
        solved_timestamp=datetime.now(timezone.utc),
        user_id=user_id,
    )

    upcoming_task = str(uuid4())
    upcoming_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-2",
        subtask_id=upcoming_subtask,
        task_id=upcoming_task,
        solved_timestamp=None,
        user_id=user_id,
    )

    recommendation = await _resolve_next_task_recommendation(user_id)
    assert recommendation is not None
    assert recommendation.lecture.id == "c1-2"
    assert recommendation.task.subtask_id == upcoming_subtask


@pytest.mark.asyncio
async def test_resolve_next_lab_prefers_latest_unsolved(mocker: MockerFixture) -> None:
    user_id = "user-labs"
    course = _build_course("course-1", ["c1-1", "c1-2"])
    courses = {"course-1": course}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=datetime(2024, 2, 1, tzinfo=timezone.utc),
            )
        )
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-2",
                completed=datetime(2024, 2, 2, tzinfo=timezone.utc),
            )
        )

    section_id = course.sections[0].id
    solved_lab_task = str(uuid4())
    solved_lab_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-1",
        subtask_id=solved_lab_subtask,
        task_id=solved_lab_task,
        solved_timestamp=datetime.now(timezone.utc),
        user_id=user_id,
        subtask_type="coding_challenge",
    )

    unsolved_lab_task = str(uuid4())
    unsolved_lab_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-2",
        subtask_id=unsolved_lab_subtask,
        task_id=unsolved_lab_task,
        solved_timestamp=None,
        user_id=user_id,
        subtask_type="coding_challenge",
    )

    labs = await get_unsolved_labs_for_lecture(
        user_id=user_id,
        course_id="course-1",
        lecture_id="c1-2",
    )
    assert [lab.subtask_id for lab in labs] == [unsolved_lab_subtask]

    recommendation = await _resolve_next_lab_recommendation(user_id)
    assert recommendation is not None
    assert recommendation.lecture.id == "c1-2"
    assert recommendation.task.subtask_type == "coding_challenge"
    assert recommendation.task.subtask_id == unsolved_lab_subtask


@pytest.mark.asyncio
async def test_resolve_next_lab_falls_back_to_unseen_lecture(mocker: MockerFixture) -> None:
    user_id = "user-labs-fallback"
    course = _build_course("course-1", ["c1-1", "c1-2"])
    courses = {"course-1": course}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=datetime(2024, 3, 1, tzinfo=timezone.utc),
            )
        )

    section_id = course.sections[0].id
    solved_lab_task = str(uuid4())
    solved_lab_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-1",
        subtask_id=solved_lab_subtask,
        task_id=solved_lab_task,
        solved_timestamp=datetime.now(timezone.utc),
        user_id=user_id,
        subtask_type="coding_challenge",
    )

    upcoming_lab_task = str(uuid4())
    upcoming_lab_subtask = str(uuid4())
    await _insert_challenge_records(
        course_id="course-1",
        section_id=section_id,
        lecture_id="c1-2",
        subtask_id=upcoming_lab_subtask,
        task_id=upcoming_lab_task,
        solved_timestamp=None,
        user_id=user_id,
        subtask_type="coding_challenge",
    )

    recommendation = await _resolve_next_lab_recommendation(user_id)
    assert recommendation is not None
    assert recommendation.lecture.id == "c1-2"
    assert recommendation.task.subtask_id == upcoming_lab_subtask


@pytest.mark.asyncio
async def test_resolve_next_lab_returns_none_when_no_labs(mocker: MockerFixture) -> None:
    user_id = "user-no-labs"
    course = _build_course("course-1", ["c1-1"])
    courses = {"course-1": course}
    mocker.patch("api.services.courses.COURSES", courses)
    mocker.patch("api.endpoints.course.COURSES", courses)

    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id=user_id,
                course_id="course-1",
                lecture_id="c1-1",
                completed=datetime(2024, 4, 1, tzinfo=timezone.utc),
            )
        )

    recommendation = await _resolve_next_lab_recommendation(user_id)
    assert recommendation is None
