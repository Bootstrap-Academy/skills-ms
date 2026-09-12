"""Chapter metadata stays consistent across the course outline and learning rooms."""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from api.schemas.course import Course
from api.schemas.rooms import Catalogue, LearningPath, Progress, RoomEnvelope, Rooms
from api.schemas.user import User
from api.services import rooms
from api.utils.docs import get_example


def chaptered_catalogue() -> dict[str, Any]:
    return {
        "paths": [
            {
                "id": "foundations",
                "title": {"de": "Grundlagen", "en": "Foundations"},
                "chapters": [
                    {"id": "data", "title": {"de": "Daten", "en": "Data"}},
                    {"id": "storage", "title": {"de": "Speicher", "en": "Storage"}},
                ],
                "units": ["bits", "bytes", "memory"],
            }
        ],
        "units": [
            {
                "id": uid,
                "path_id": "foundations",
                "chapter_id": chapter_id,
                "title": {"de": uid, "en": uid},
                "room": "guided-lesson",
                "content": {},
                "teaches": [],
                "practices": [],
                "requires": [],
                "retired": False,
                "completion": {"kind": "introduced", "answer": {"answer": uid}},
            }
            for uid, chapter_id in (("bits", "data"), ("bytes", "data"), ("memory", "storage"))
        ],
    }


@pytest.mark.parametrize(
    ("chapters", "unit_chapters", "message"),
    [
        (["data", "data"], ["data", "data", "data"], "Duplicate learning-chapter"),
        (["data", "storage"], [None, "data", "storage"], "must belong to a chapter"),
        (["data", "storage"], ["data", "other", "storage"], "Invalid learning-chapter"),
        (["data", "storage"], ["data", "storage", "data"], "contiguous chapters"),
        (["data", "storage"], ["storage", "storage", "data"], "declared order"),
        ([], ["data", "data", "storage"], "Invalid learning-chapter"),
    ],
)
def test_invalid_chapter_layout_is_rejected(chapters: list[str], unit_chapters: list[str | None], message: str) -> None:
    data = chaptered_catalogue()
    data["paths"][0]["chapters"] = [{"id": cid, "title": {"de": cid, "en": cid}} for cid in chapters]
    for unit, cid in zip(data["units"], unit_chapters, strict=True):
        unit["chapter_id"] = cid
    with pytest.raises(ValidationError, match=message):
        Catalogue.parse_obj(data)


def test_legacy_paths_need_no_chapters() -> None:
    data = chaptered_catalogue()
    del data["paths"][0]["chapters"]
    for unit in data["units"]:
        del unit["chapter_id"]
    content = Catalogue.parse_obj(data)
    assert content.paths[0].chapters == []
    assert all(unit.public().chapter_id is None for unit in content.units)


def test_chapters_are_local_to_their_path_and_do_not_depend_on_catalogue_storage_order() -> None:
    data = chaptered_catalogue()
    other = chaptered_catalogue()
    other["paths"][0]["id"] = "other-foundations"
    other["paths"][0]["units"] = [f"other-{uid}" for uid in other["paths"][0]["units"]]
    for unit in other["units"]:
        unit["path_id"] = "other-foundations"
        unit["id"] = f"other-{unit['id']}"
    data["paths"].extend(other["paths"])
    data["units"].extend(reversed(other["units"]))
    content = Catalogue.parse_obj(data)
    assert len(content.paths) == 2
    assert content.paths[0].chapters == content.paths[1].chapters


async def test_course_outline_and_next_room_keep_the_same_chapter_metadata(monkeypatch: MonkeyPatch) -> None:
    content = Catalogue.parse_obj(chaptered_catalogue())
    path = LearningPath.parse_obj(content.paths[0].dict(exclude={"units"}))
    selected = RoomEnvelope(unit=content.units[0].public(), progress=Progress())
    monkeypatch.setattr(rooms, "catalogue", lambda: content)
    monkeypatch.setattr(rooms, "require_path_access", AsyncMock())
    monkeypatch.setattr(rooms, "read_states", AsyncMock(return_value={}))
    monkeypatch.setattr(rooms, "next_room", AsyncMock(return_value=Rooms(paths=[path], path=path, next=selected)))
    course = Course.parse_obj({**get_example(Course), "learning_path_id": "foundations"})
    result = await rooms.course_learning(course, User(id="learner", email_verified=True, admin=False), "synthetic")
    assert [chapter.dict() for chapter in result.path.chapters] == chaptered_catalogue()["paths"][0]["chapters"]
    assert [unit.chapter_id for unit in result.units] == ["data", "data", "storage"]
    assert result.next is not None and result.next.unit.chapter_id == "data"
    assert all(unit.status == "new" and unit.result is None for unit in result.units)
    assert result.completed is False
