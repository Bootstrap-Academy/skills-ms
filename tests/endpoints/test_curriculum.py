"""New lesson composition preserves existing private work and completion authorities."""

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import ValidationError
from pytest import MonkeyPatch

from api import models
from api.auth import public_auth, user_auth
from api.database import db, db_context, filter_by
from api.endpoints import course as course_endpoints
from api.endpoints.curriculum import router
from api.endpoints.rooms import router as rooms_router
from api.schemas.course import Course
from api.schemas.curriculum import ActivityReference, CurriculumDefinition
from api.schemas.lesson_module import LessonModuleDescriptor
from api.schemas.rooms import Catalogue, CatalogueUnit
from api.schemas.user import User
from api.services import curriculum, rooms
from api.services.courses import COURSES
from api.services.lesson_modules import register_module
from api.settings import settings
from api.utils.utc import utcnow
from tests.endpoints.test_rooms import content as room_content
from tests.endpoints.test_rooms import payload


content = room_content  # Keep the existing synthetic catalogue fixture.


def course_definition(**values: Any) -> Course:
    return Course.parse_obj(
        {
            "id": "composed",
            "title": "Synthetic course",
            "description": None,
            "category": None,
            "language": "de",
            "image": None,
            "authors": [],
            "price": 0,
            "learning_goals": [],
            "requirements": [],
            "last_update": 0,
            "learning_path_id": "python-loops",
            **values,
        }
    )


def composed(*unit_ids: str) -> dict[str, Any]:
    return {
        "lessons": [
            {
                "id": "combined",
                "title": {"de": "Gemeinsam", "en": "Together"},
                "activities": [{"id": uid, "source": {"kind": "room", "unit_id": uid}} for uid in unit_ids],
            }
        ]
    }


@pytest.fixture
async def curriculum_client(content: Catalogue, monkeypatch: MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    course = course_definition(curriculum=composed("intro", "exercise"))
    monkeypatch.setitem(COURSES, course.id, course)
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))

    async def session() -> AsyncIterator[None]:
        async with db_context():
            yield

    async def identity(request: Request) -> User:
        token = request.headers.get("Authorization")
        if token not in ("Bearer subject-a", "Bearer subject-b"):
            raise HTTPException(401, "Synthetic missing authority")
        return User(id=token.removeprefix("Bearer "), email_verified=True, admin=False)

    app = FastAPI()
    app.dependency_overrides[user_auth.dependency] = identity

    async def optional_identity(request: Request) -> User | None:
        return await identity(request) if request.headers.get("Authorization") else None

    app.dependency_overrides[public_auth.dependency] = optional_identity
    for routes in (router, rooms_router, course_endpoints.router):
        app.include_router(routes, dependencies=[Depends(session)])
    async with httpx.AsyncClient(
        app=app, base_url="http://synthetic", headers={"Authorization": "Bearer subject-a"}
    ) as client:
        yield client


async def test_composed_lesson_keeps_order_state_review_and_one_completion(
    curriculum_client: httpx.AsyncClient,
) -> None:
    client = curriculum_client
    outline = await client.get("/courses/composed/curriculum")
    assert outline.status_code == 200 and outline.headers["cache-control"] == "private, no-store"
    assert outline.json()["explicit"] is True
    assert outline.json()["chapters"] == []
    assert outline.json()["lessons"][0]["activity_ids"] == ["intro", "exercise"]
    assert "content" not in outline.text and "state" not in outline.text

    save = payload(state={"private": "original-shape"})
    assert (await client.put("/rooms/intro/state?course=composed", json=save)).status_code == 200
    lesson = (await client.get("/courses/composed/lessons/combined")).json()
    assert [activity["id"] for activity in lesson["activities"]] == ["intro", "exercise"]
    assert lesson["activities"][0]["progress"]["state"] == {"private": "original-shape"}
    assert lesson["activities"][0]["kind"] == "legacy-room"
    assert lesson["activities"][1]["kind"] == "coding"
    assert lesson["completed"] is False
    finish = payload(revision=1, action="complete", answer={"answer": 6})
    first = await client.post("/rooms/intro/complete?course=composed", json=finish)
    assert first.status_code == 200
    assert (await client.post("/rooms/intro/complete?course=composed", json=finish)).json() == first.json()
    assert (
        await client.post("/rooms/exercise/complete?course=composed", json=payload(action="complete"))
    ).status_code == 200
    assert (await client.get("/courses/composed/lessons/combined")).json()["completed"] is True
    assert (await client.get("/courses/composed/curriculum")).json()["lessons"][0]["completed"] is True

    review = await client.post("/rooms/intro/review?course=composed", json=payload(revision=2))
    assert review.status_code == 200
    reviewed = (await client.get("/courses/composed/lessons/combined")).json()
    assert reviewed["completed"] is True  # Historical course completion survives a new review.
    assert reviewed["activities"][0]["progress"]["status"] == "in_progress"
    other = (
        await client.get("/courses/composed/lessons/combined", headers={"Authorization": "Bearer subject-b"})
    ).json()
    assert other["completed"] is False and other["activities"][0]["progress"]["state"] == {}
    async with db_context():
        assert await db.all(filter_by(models.XPOperation, user_id="subject-a")) == []
        assert await db.all(filter_by(models.LectureProgress, user_id="subject-a")) == []
        overrides = await curriculum.completion_overrides(
            User(id="subject-a", email_verified=True, admin=False), [COURSES["composed"]]
        )
        assert overrides == {"composed": True}


async def test_curriculum_admission_missing_lesson_and_read_only_state(
    curriculum_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    client = curriculum_client
    assert (await client.get("/courses/composed/curriculum", headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/courses/composed/lessons/missing")).status_code == 404
    assert (await client.get("/courses/composed/curriculum")).status_code == 200

    async with db_context():
        assert await db.all(filter_by(models.RoomState, user_id="subject-a")) == []
        assert await db.all(filter_by(models.PurchaseUser, user_id="subject-a")) == []
    monkeypatch.setattr(course_endpoints, "has_premium", AsyncMock(return_value=False))
    monkeypatch.setitem(COURSES, "composed", course_definition(price=5, curriculum=composed("intro")))
    assert (await client.get("/courses/composed/curriculum")).status_code == 403
    async with db_context():
        await db.add(models.CourseAccess(user_id="subject-a", course_id="composed"))
    assert (await client.get("/courses/composed/curriculum")).status_code == 200


@pytest.mark.parametrize("unavailable", ["rooms_disabled", "missing_room", "retired_room"])
async def test_unavailable_curriculum_does_not_hide_other_courses_or_reuse_video_completion(
    curriculum_client: httpx.AsyncClient, monkeypatch: MonkeyPatch, unavailable: str
) -> None:
    sections = [
        {
            "id": "videos",
            "title": "Videos",
            "lectures": [{"id": "old", "title": "Video", "type": "youtube", "video_id": "dQw4w9WgXcQ", "duration": 20}],
        }
    ]
    legacy = course_definition(id="legacy", learning_path_id=None, sections=sections)
    healthy = course_definition(
        id="healthy",
        learning_path_id=None,
        sections=sections,
        curriculum={
            "lessons": [
                {
                    "id": "video",
                    "title": {"de": "Video", "en": "Video"},
                    "activities": [
                        {
                            "id": "old",
                            "source": {
                                "kind": "lecture",
                                "course_id": "healthy",
                                "section_id": "videos",
                                "lecture_id": "old",
                            },
                        }
                    ],
                }
            ]
        },
    )
    unit = {"rooms_disabled": "intro", "missing_room": "missing", "retired_room": "retired"}[unavailable]
    broken = course_definition(sections=sections, curriculum=composed(unit))
    courses = {course.id: course for course in (legacy, healthy, broken)}
    monkeypatch.setattr(course_endpoints, "COURSES", courses)
    monkeypatch.setattr(rooms, "COURSES", courses)
    if unavailable == "rooms_disabled":
        monkeypatch.setattr(settings, "rooms_enabled", False)
    async with db_context():
        for course in courses.values():
            await db.add(
                models.LectureProgress(user_id="subject-a", course_id=course.id, lecture_id="old", completed=utcnow())
            )

    response = await curriculum_client.get("/courses")
    assert response.status_code == 200
    summaries = {item["id"]: item for item in response.json()}
    assert summaries["legacy"]["completed"] is True
    assert summaries["healthy"]["completed"] is True
    assert summaries["composed"]["completed"] is None
    single = await curriculum_client.get("/courses/composed/summary")
    assert single.status_code == 200 and single.json()["completed"] is None
    # Only the unavailable course's detailed content remains unavailable.
    outline = await curriculum_client.get("/courses/composed/curriculum")
    assert outline.status_code == (404 if unavailable == "rooms_disabled" else 503)
    async with db_context():
        assert await db.all(filter_by(models.RoomState, user_id="subject-a")) == []
        assert await db.all(filter_by(models.XPOperation, user_id="subject-a")) == []
        assert len(await db.all(filter_by(models.LectureProgress, user_id="subject-a"))) == 3


async def test_unavailable_curriculum_still_rejects_erased_user(
    curriculum_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rooms_enabled", False)
    monkeypatch.setattr(course_endpoints, "COURSES", {"composed": COURSES["composed"]})
    async with db_context():
        await db.add(models.PurchaseUser(user_id="subject-a", deleted=True))
    assert (await curriculum_client.get("/courses")).status_code == 401
    assert (await curriculum_client.get("/courses/composed/summary")).status_code == 401


async def test_legacy_video_keeps_lecture_ids_progress_and_deferred_practice(
    curriculum_client: httpx.AsyncClient, monkeypatch: MonkeyPatch
) -> None:
    legacy = course_definition(
        learning_path_id=None,
        sections=[
            {
                "id": "chapter_one",
                "title": "Chapter",
                "description": None,
                "lectures": [
                    {
                        "id": "original_lecture",
                        "title": "Video",
                        "description": None,
                        "type": "youtube",
                        "video_id": "dQw4w9WgXcQ",
                        "duration": 20,
                    }
                ],
            }
        ],
    )
    monkeypatch.setitem(COURSES, legacy.id, legacy)
    async with db_context():
        await db.add(
            models.LectureProgress(
                user_id="subject-a", course_id=legacy.id, lecture_id="original_lecture", completed=utcnow()
            )
        )
    lesson = (await curriculum_client.get("/courses/composed/lessons/original_lecture")).json()
    assert lesson["explicit"] is False and lesson["completed"] is True
    assert lesson["activities"][0]["id"] == "original_lecture"
    assert lesson["activities"][0]["source"]["kind"] == "lecture"
    assert lesson["legacy_practice"] == {
        "course_id": "composed",
        "section_id": "chapter_one",
        "lecture_id": "original_lecture",
    }
    async with db_context():
        assert await db.all(filter_by(models.RoomState, user_id="subject-a")) == []


async def test_custom_registry_and_video_work_through_existing_room_mutations(
    curriculum_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    custom = CatalogueUnit.parse_obj(
        {
            "id": "own-simulation",
            "path_id": "python-loops",
            "title": {"de": "Simulation", "en": "Simulation"},
            "room": "custom",
            "module_id": "fixture-simulation",
            "content": {"de": {}, "en": {}},
            "teaches": [],
            "practices": [],
            "requires": [],
            "retired": False,
            "completion": {"kind": "introduced", "answer": {"observed": True}, "allow_skip": False},
        }
    )
    video = CatalogueUnit.parse_obj(
        {
            "id": "new-video",
            "path_id": "python-loops",
            "title": {"de": "Video", "en": "Video"},
            "room": "video",
            "content": {language: {"video": {"type": "youtube", "id": "dQw4w9WgXcQ"}} for language in ("de", "en")},
            "teaches": [],
            "practices": [],
            "requires": [],
            "retired": False,
            "completion": {"kind": "introduced", "answer": {"viewed": True}, "allow_skip": True},
        }
    )
    content.units.extend([custom, video])
    content.paths[0].units.extend([custom.id, video.id])
    monkeypatch.setattr(settings, "lesson_module_origins", ["https://modules.example"])
    monkeypatch.setitem(COURSES, "composed", course_definition(curriculum=composed(custom.id, video.id)))
    assert (await curriculum_client.get("/rooms/own-simulation?course=composed")).status_code == 503
    descriptor = LessonModuleDescriptor(
        id="fixture-simulation", api_version=1, entry_url="https://modules.example/hash/main.mjs"
    )
    async with db_context():
        await register_module(descriptor)
    fetched = (await curriculum_client.get("/rooms?course=composed&unit=own-simulation")).json()
    assert fetched["next"]["unit"]["module"] == descriptor.dict()
    lesson = (await curriculum_client.get("/courses/composed/lessons/combined")).json()
    assert [activity["kind"] for activity in lesson["activities"]] == ["custom", "video"]
    assert lesson["activities"][0]["module"] == descriptor.dict()
    completed_video = await curriculum_client.post(
        "/rooms/new-video/complete?course=composed", json=payload(action="complete", answer={"viewed": True})
    )
    assert completed_video.status_code == 200 and completed_video.json()["progress"]["result"] == {"kind": "introduced"}
    monkeypatch.setattr(settings, "lesson_module_origins", [])
    assert (await curriculum_client.get("/rooms/own-simulation?course=composed")).status_code == 503


def test_explicit_definition_rejects_unbound_assessment_and_duplicate_work() -> None:
    with pytest.raises(ValidationError, match="room with an ExerciseRef"):
        ActivityReference.parse_obj({"id": "new", "source": {"kind": "challenge"}})
    with pytest.raises(ValidationError, match="existing unit ID"):
        ActivityReference.parse_obj({"id": "copy", "source": {"kind": "room", "unit_id": "old"}})
    with pytest.raises(ValidationError, match="same activity twice"):
        CurriculumDefinition.parse_obj(composed("intro", "intro"))


def test_legacy_catalogue_projection_preserves_every_course_and_unit(content: Catalogue) -> None:
    # Current repository definitions contain no native authored curriculum yet.
    for course in COURSES.values():
        if course.learning_path_id is None:
            definition, _ = curriculum.definitions(course)
            assert [lesson.id for lesson in definition.lessons] == [
                lecture.id for section in course.sections for lecture in section.lectures
            ]
    definition, _ = curriculum.definitions(course_definition())
    assert [lesson.id for lesson in definition.lessons] == [uid for uid in content.paths[0].units if uid != "retired"]


def test_mixed_legacy_id_collision_only_qualifies_presentation_ids(content: Catalogue) -> None:
    course = course_definition(
        sections=[
            {
                "id": "intro",
                "title": "Chapter",
                "lectures": [
                    {"id": "intro", "title": "Video", "type": "youtube", "video_id": "dQw4w9WgXcQ", "duration": 20}
                ],
            }
        ]
    )
    definition, _ = curriculum.definitions(course)
    assert definition.lessons[0].id == "intro"
    legacy = definition.lessons[-1]
    assert legacy.id != "intro"
    assert legacy.activities[0].source.dict() == {
        "kind": "lecture",
        "course_id": course.id,
        "section_id": "intro",
        "lecture_id": "intro",
    }
    assert len({ref.id for lesson in definition.lessons for ref in lesson.activities}) == sum(
        len(lesson.activities) for lesson in definition.lessons
    )


async def test_custom_assessment_uses_existing_proof_and_review_authority(
    curriculum_client: httpx.AsyncClient, content: Catalogue, monkeypatch: MonkeyPatch
) -> None:
    exercise = next(unit for unit in content.units if unit.id == "exercise")
    exercise.room = "custom"
    exercise.module_id = "fixture-assessment"
    monkeypatch.setattr(settings, "lesson_module_origins", ["https://modules.example"])
    async with db_context():
        await register_module(
            LessonModuleDescriptor(
                id="fixture-assessment", api_version=1, entry_url="https://modules.example/hash/main.mjs"
            )
        )
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=False))
    client = curriculum_client
    failed = await client.post(
        "/rooms/exercise/complete?course=composed", json=payload(action="complete", answer={"solved": True})
    )
    assert failed.status_code == 409
    monkeypatch.setattr(rooms, "challenge_status", AsyncMock(return_value=True))
    first = await client.post("/rooms/exercise/complete?course=composed", json=payload(action="complete"))
    assert first.status_code == 200 and first.json()["progress"]["result"] == {"kind": "solved"}
    start = payload(1)
    assert (await client.post("/rooms/exercise/review?course=composed", json=start)).status_code == 200
    finish = payload(2, action="complete", review_id=start["request_id"], attempt_id=str(uuid4()))
    verify = AsyncMock(return_value=False)
    monkeypatch.setattr(rooms, "review_attempt_solved", verify)
    assert (await client.post("/rooms/exercise/complete?course=composed", json=finish)).status_code == 409
    verify.return_value = True
    result = await client.post("/rooms/exercise/complete?course=composed", json=finish)
    assert result.status_code == 200 and result.json()["progress"]["result"] == {"kind": "solved"}
    assert (await client.post("/rooms/exercise/complete?course=composed", json=finish)).json() == result.json()
    assert verify.await_count == 2  # replay uses the original receipt
    async with db_context():
        assert await db.all(filter_by(models.XPOperation, user_id="subject-a")) == []


@pytest.mark.parametrize(
    "source", [None, {"video": []}, {"video": {"type": "youtube", "id": 5}}, {"video": {"type": "mp4", "url": None}}]
)
def test_native_video_rejects_malformed_content(source: Any) -> None:
    with pytest.raises(ValidationError, match="video requires"):
        CatalogueUnit.parse_obj(
            {
                "id": "video",
                "path_id": "path",
                "title": {"de": "Video", "en": "Video"},
                "room": "video",
                "content": {"de": source, "en": source},
                "teaches": [],
                "practices": [],
                "requires": [],
                "retired": False,
                "completion": {"kind": "introduced", "answer": {"viewed": True}},
            }
        )
