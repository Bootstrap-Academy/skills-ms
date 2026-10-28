"""The learner's own learning plan ("Dein Lernweg"), its suggestions and the learner's view of a track.

The plan is one row per learner, private to them and erased with the account. Changes are guarded by
`expected_revision` alone; the latest `request_id` with the fingerprint of its request lets an exact retry get the
same answer. Plan contents never go into log lines or error reports: refusals name a code and a step position only.
"""

import json
from hashlib import sha256
from typing import Any, cast

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from api.database import db, filter_by
from api.exceptions.learning_plan import PlanRefusal
from api.models import XP, LearningPlan, RootSkill, SkillCourse, SubSkill, SubSkillDependency
from api.models.root_skill import RootSkillDependency
from api.schemas.learning_plan import (
    PLAN_STATE_LIMIT,
    CourseStepState,
    ExternalStepState,
    Plan,
    PlanCourseStep,
    PlanExternalStep,
    PlanNextCourse,
    PlanNextExternal,
    PlanState,
    SavePlan,
    Suggestion,
)
from api.schemas.tracks import (
    CourseItemDefinition,
    ExternalDefinition,
    ExternalItemDefinition,
    Language,
    TrackDefinition,
    UserTrack,
)
from api.schemas.user import User
from api.services import recommendations
from api.services.course_progress import course_progress
from api.services.courses import COURSES
from api.services.purchases import lock_user, read_user_guard
from api.services.tracks import TRACKS, course_title, user_track
from api.utils.utc import utcnow


def normalized(state: PlanState) -> dict[str, Any]:
    """The state as stored: unset optional fields left out, so equal plans are equal JSON."""
    return state.model_dump(mode="json", exclude_none=True)


def state_size(state: dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def request_fingerprint(expected_revision: int, state: dict[str, Any]) -> str:
    payload = {"expected_revision": expected_revision, "state": state}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stage_course(track_id: str | None, stage_id: str | None, course_id: str) -> bool:
    track = TRACKS.get(track_id or "")
    stage = None if track is None else track.stage(stage_id)
    return stage is not None and any(
        isinstance(item, CourseItemDefinition) and item.course == course_id for item in stage.items
    )


def stage_external(track_id: str, stage_id: str, external_id: str) -> ExternalDefinition | None:
    track = TRACKS.get(track_id)
    stage = None if track is None else track.stage(stage_id)
    items = [] if stage is None else stage.items
    return next(
        (
            item.external
            for item in items
            if isinstance(item, ExternalItemDefinition) and item.external.id == external_id
        ),
        None,
    )


def refuse(index: int, code: str, message: str) -> PlanRefusal:
    return PlanRefusal(422, code, message, {"step": index})


def check_steps(state: PlanState) -> None:
    """422 for a step that names an unknown course, track, stage or step, or repeats an earlier step."""
    seen: set[tuple[str, ...]] = set()
    for index, step in enumerate(state.steps):
        track = None if step.track is None else TRACKS.get(step.track)
        if isinstance(step, CourseStepState):
            if step.course not in COURSES:
                raise refuse(index, "unknown_course", "This course does not exist")
            key: tuple[str, ...] = ("course", step.course)
            linked = step.track is not None or step.stage is not None
        else:
            key = ("external", step.track, step.stage, step.id)
            linked = True
        if linked:
            if track is None:
                raise refuse(index, "unknown_track", "This track does not exist")
            if track.stage(step.stage) is None:
                raise refuse(index, "unknown_stage", "This track has no such stage")
            if isinstance(step, CourseStepState):
                if not stage_course(step.track, step.stage, step.course):
                    raise refuse(index, "unknown_step", "This stage has no such course")
            elif stage_external(step.track, step.stage, step.id) is None:
                raise refuse(index, "unknown_step", "This stage has no such step")
        if key in seen:
            raise refuse(index, "duplicate_step", "This step is already in the plan")
        seen.add(key)


async def require_account(user: User) -> None:
    # Reads never create the durable user lock or a plan row.
    guard = await read_user_guard(user.id)
    if guard is not None and guard.deleted:
        raise HTTPException(401, "This account is no longer available")


async def read_row(user_id: str) -> LearningPlan | None:
    row: LearningPlan | None = await db.first(
        filter_by(LearningPlan, user_id=user_id).execution_options(populate_existing=True)
    )
    return row


def stored_state(row: LearningPlan | None) -> PlanState:
    if row is None:
        return PlanState()
    try:
        return PlanState.model_validate(row.state)
    except ValidationError:
        # The error would quote the plan; it must not reach a log line or an error report.
        raise HTTPException(503, "The learning plan cannot be read right now") from None


async def render(user: User, revision: int, state: PlanState, language: Language) -> Plan:
    """The plan with titles and progress. A step whose course or external step no longer exists is left out; a
    course keeps its place without the track when the track no longer holds it."""
    progress = await course_progress(user, [step.course for step in state.steps if isinstance(step, CourseStepState)])
    steps: list[PlanCourseStep | PlanExternalStep] = []
    for step in state.steps:
        if isinstance(step, CourseStepState):
            if step.course not in COURSES:
                continue
            linked = stage_course(step.track, step.stage, step.course)
            course = progress[step.course]
            steps.append(
                PlanCourseStep(
                    course=step.course,
                    title=course_title(COURSES[step.course], language),
                    track=step.track if linked else None,
                    stage=step.stage if linked else None,
                    status=course.status,
                    done_units=course.done_units,
                    total_units=course.total_units,
                )
            )
        elif (external := stage_external(step.track, step.stage, step.id)) is not None:
            steps.append(
                PlanExternalStep(
                    id=step.id,
                    title=external.title.text(language),
                    url=external.url,
                    track=step.track,
                    stage=step.stage,
                    status="done" if step.done else "open",
                    self_confirmed=step.done,
                )
            )
    first = next((step for step in steps if step.status != "done"), None)
    return Plan(
        revision=revision,
        character=state.character,
        preference=state.preference,
        steps=steps,
        next=(
            None
            if first is None
            else (
                PlanNextCourse(course=first.course)
                if isinstance(first, PlanCourseStep)
                else PlanNextExternal(id=first.id, track=first.track, stage=first.stage)
            )
        ),
    )


async def get_plan(user: User, language: Language) -> Plan:
    await require_account(user)
    row = await read_row(user.id)
    return await render(user, 0 if row is None else row.revision, stored_state(row), language)


async def conflict(user: User, language: Language, code: str, message: str) -> PlanRefusal:
    row = await read_row(user.id)
    current = await render(user, 0 if row is None else row.revision, stored_state(row), language)
    return PlanRefusal(409, code, message, {"plan": current.model_dump(mode="json")})


async def save_plan(user: User, data: SavePlan, language: Language) -> Plan:
    # The erasure lock, as for room states and projects: a save admitted before erasure cannot recreate the plan.
    guard = await lock_user(user.id)
    if guard.deleted:
        raise HTTPException(401, "This account is no longer available")
    state = normalized(data.state)
    if state_size(state) > PLAN_STATE_LIMIT:
        raise PlanRefusal(413, "plan_too_large", "The learning plan is larger than 16 KiB")
    fingerprint = request_fingerprint(data.expected_revision, state)
    row = await read_row(user.id)
    if row is not None and row.request_id == str(data.request_id):
        if row.request_fingerprint == fingerprint:
            # The latest save, repeated: the same answer, nothing changes.
            return await render(user, row.revision, stored_state(row), language)
        raise await conflict(user, language, "request_reused", "This request was already used for another change")
    check_steps(data.state)
    if (0 if row is None else row.revision) != data.expected_revision:
        raise await conflict(user, language, "plan_conflict", "Your learning plan has changed in another session")
    now = utcnow()
    revision = data.expected_revision + 1
    values: dict[str, Any] = {
        "revision": revision,
        "state": state,
        "request_id": str(data.request_id),
        "request_fingerprint": fingerprint,
        "updated_at": now,
    }
    if row is None:
        await db.add(LearningPlan(user_id=user.id, **values))
    else:
        result = await db.exec(
            update(LearningPlan)
            .where(LearningPlan.user_id == user.id, LearningPlan.revision == data.expected_revision)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if cast(CursorResult, result).rowcount != 1:
            raise await conflict(user, language, "plan_conflict", "Your learning plan has changed in another session")
    try:
        await db.session.flush()
    except IntegrityError:
        # The user lock serializes saves on PostgreSQL/MySQL; the primary key also protects databases without it.
        await db.session.rollback()
        raise await conflict(
            user, language, "plan_conflict", "Your learning plan has changed in another session"
        ) from None
    return await render(user, revision, PlanState.model_validate(state), language)


async def track_for_user(user: User, track: TrackDefinition, language: Language) -> UserTrack:
    await require_account(user)
    progress = await course_progress(user, track.course_ids())
    confirmed = {
        (step.track, step.stage, step.id)
        for step in stored_state(await read_row(user.id)).steps
        if isinstance(step, ExternalStepState) and step.done
    }
    return user_track(track, language, progress, confirmed)


async def skill_graph() -> recommendations.SkillGraph:
    roots: dict[str, set[str]] = {root: set() for root in await db.all(select(RootSkill.id))}
    for dependent, dependency in (
        await db.exec(select(RootSkillDependency.dependent_id, RootSkillDependency.dependency_id))
    ).all():
        roots.setdefault(dependent, set()).add(dependency)
    sub_skill_roots: dict[str, str] = {}
    for sub, root in (await db.exec(select(SubSkill.id, SubSkill.parent_id))).all():
        sub_skill_roots[sub] = root
    sub_dependencies: dict[str, set[str]] = {}
    for dependent, dependency in (
        await db.exec(select(SubSkillDependency.dependent_id, SubSkillDependency.dependency_id))
    ).all():
        sub_dependencies.setdefault(dependent, set()).add(dependency)
    course_skills: dict[str, set[str]] = {}
    for skill, course in (await db.exec(select(SkillCourse.skill_id, SkillCourse.course_id))).all():
        course_skills.setdefault(course, set()).add(skill)
    return recommendations.SkillGraph(
        root_dependencies={root: frozenset(deps) for root, deps in roots.items()},
        sub_skill_roots=sub_skill_roots,
        sub_skill_dependencies={sub: frozenset(deps) for sub, deps in sub_dependencies.items()},
        course_skills={course: frozenset(skills) for course, skills in course_skills.items()},
    )


async def suggestions(user: User, language: Language) -> list[Suggestion]:
    """Up to six suggestions, computed for this answer only; nothing about them is stored."""
    await require_account(user)
    state = stored_state(await read_row(user.id))
    planned = [step.course for step in state.steps if isinstance(step, CourseStepState) and step.course in COURSES]
    tracks = [step.track for step in state.steps if step.track is not None and step.track in TRACKS]
    graph = await skill_graph()
    xp_skills = frozenset(await db.all(select(XP.skill_id).where(XP.user_id == user.id, XP.xp > 0)))
    progress = await course_progress(user, [*graph.course_skills, *planned])
    learner = recommendations.Learner(
        character=state.character,
        preference=state.preference,
        planned=planned,
        tracks=list(dict.fromkeys(tracks)),
        xp_skills=xp_skills,
        completed=frozenset(course for course, value in progress.items() if value.status == "done"),
    )
    found = recommendations.suggest(
        graph,
        learner,
        {course_id: course.level for course_id, course in COURSES.items()},
        {track_id: track.course_ids() for track_id, track in TRACKS.items()},
    )
    return [
        Suggestion(
            course=item.course,
            title=course_title(COURSES[item.course], language),
            reason=item.reason,
            reason_course=item.reason_course,
            reason_track=item.reason_track,
        )
        for item in found
    ]
