from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import and_, select

from api.database import challenges as challenges_db


QUIZ_SUBTASK_TYPES = {"matching", "multiple_choice_question", "question"}
LAB_SUBTASK_TYPES = {"coding_challenge"}


@dataclass(slots=True)
class SubtaskRecommendation:
    task_id: str
    subtask_id: str
    subtask_type: str


async def _get_unsolved_subtasks_for_lecture(
    *,
    user_id: str,
    course_id: str,
    lecture_id: str,
    allowed_types: Iterable[str],
) -> list[SubtaskRecommendation]:
    """Return unsolved subtasks of the given types for a lecture."""

    if not challenges_db.challenges_configured():
        return []

    allowed = {ty.lower() for ty in allowed_types}
    if not allowed:
        return []

    try:
        async with challenges_db.challenges_session() as session:
            stmt = (
                select(
                    challenges_db.challenges_subtasks.c.task_id,
                    challenges_db.challenges_subtasks.c.id.label("subtask_id"),
                    challenges_db.challenges_subtasks.c.ty,
                )
                .select_from(
                    challenges_db.challenges_course_tasks.join(
                        challenges_db.challenges_subtasks,
                        challenges_db.challenges_course_tasks.c.task_id
                        == challenges_db.challenges_subtasks.c.task_id,
                    ).outerjoin(
                        challenges_db.challenges_user_subtasks,
                        and_(
                            challenges_db.challenges_user_subtasks.c.subtask_id
                            == challenges_db.challenges_subtasks.c.id,
                            challenges_db.challenges_user_subtasks.c.user_id == user_id,
                        ),
                    )
                )
                .where(
                    challenges_db.challenges_course_tasks.c.course_id == course_id,
                    challenges_db.challenges_course_tasks.c.lecture_id == lecture_id,
                    challenges_db.challenges_subtasks.c.enabled.is_(True),
                    challenges_db.challenges_subtasks.c.retired.is_(False),
                    challenges_db.challenges_subtasks.c.ty.in_(allowed),
                    challenges_db.challenges_user_subtasks.c.solved_timestamp.is_(None),
                )
                .order_by(
                    challenges_db.challenges_subtasks.c.creation_timestamp.asc(),
                    challenges_db.challenges_subtasks.c.id.asc(),
                )
            )
            rows = (await session.execute(stmt)).mappings().all()
    except RuntimeError:
        return []

    return [
        SubtaskRecommendation(
            task_id=row["task_id"],
            subtask_id=row["subtask_id"],
            subtask_type=row["ty"],
        )
        for row in rows
    ]


async def get_unsolved_quizzes_for_lecture(
    *,
    user_id: str,
    course_id: str,
    lecture_id: str,
    allowed_types: Iterable[str] | None = None,
) -> list[SubtaskRecommendation]:
    """Return unsolved quiz-like subtasks for the lecture."""

    return await _get_unsolved_subtasks_for_lecture(
        user_id=user_id,
        course_id=course_id,
        lecture_id=lecture_id,
        allowed_types=allowed_types or QUIZ_SUBTASK_TYPES,
    )


async def get_unsolved_labs_for_lecture(
    *,
    user_id: str,
    course_id: str,
    lecture_id: str,
    allowed_types: Iterable[str] | None = None,
) -> list[SubtaskRecommendation]:
    """Return unsolved lab-style subtasks (coding/hacking challenges) for the lecture."""

    return await _get_unsolved_subtasks_for_lecture(
        user_id=user_id,
        course_id=course_id,
        lecture_id=lecture_id,
        allowed_types=allowed_types or LAB_SUBTASK_TYPES,
    )


__all__ = [
    "LAB_SUBTASK_TYPES",
    "QUIZ_SUBTASK_TYPES",
    "SubtaskRecommendation",
    "get_unsolved_labs_for_lecture",
    "get_unsolved_quizzes_for_lecture",
]
