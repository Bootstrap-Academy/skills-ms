"""Every actual repository course projects without rewriting any source reference."""

from pytest import MonkeyPatch

from api.schemas.curriculum import LectureSource, RoomSource
from api.services import curriculum, rooms
from api.services.courses import COURSES
from api.settings import settings


def test_all_existing_courses_and_rooms_keep_their_source_order(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "rooms_enabled", True)
    content = rooms.catalogue()
    units = {unit.id: unit for unit in content.units if not unit.retired}
    paths = {path.id: path for path in content.paths}
    for course in COURSES.values():
        if course.curriculum is not None:
            continue
        definition, _ = curriculum.definitions(course, content)
        expected_rooms = (
            []
            if course.learning_path_id is None
            else [uid for uid in paths[course.learning_path_id].units if uid in units]
        )
        expected_lectures = [
            (course.id, section.id, lecture.id) for section in course.sections for lecture in section.lectures
        ]
        refs = [ref for lesson in definition.lessons for ref in lesson.activities]
        assert [ref.source.unit_id for ref in refs if isinstance(ref.source, RoomSource)] == expected_rooms, course.id
        assert [
            (ref.source.course_id, ref.source.section_id, ref.source.lecture_id)
            for ref in refs
            if isinstance(ref.source, LectureSource)
        ] == expected_lectures, course.id
        assert "curriculum" not in course.summary(None).dict()
        assert course.summary(None).has_explicit_curriculum is False
