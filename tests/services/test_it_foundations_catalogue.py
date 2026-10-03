"""Check the complete IT learning path and the boundary between labs and grading."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from api.schemas.rooms import Complete, Progress
from api.services.courses import COURSES
from api.services.rooms import completed_progress, load_catalogue


def test_it_course_has_ordered_chapters_and_a_complete_non_video_path() -> None:
    content = load_catalogue()
    course = COURSES["it-foundations"]
    path = next(path for path in content.paths if path.id == course.learning_path_id)
    by_id = {unit.id: unit for unit in content.units}
    units = [by_id[uid] for uid in path.units]
    assert course.free and not course.sections and course.requirements == []
    assert len(path.chapters) == 9 and len(units) == 50
    assert all(unit.chapter_id in {chapter.id for chapter in path.chapters} for unit in units)
    assert all(any(unit.chapter_id == chapter.id for unit in units) for chapter in path.chapters)
    assert [
        unit.chapter_id
        for index, unit in enumerate(units)
        if index == 0 or units[index - 1].chapter_id != unit.chapter_id
    ] == [chapter.id for chapter in path.chapters]
    assert sum(unit.room == "exercise" for unit in units) == 24
    assert {unit.room for unit in units if unit.room != "exercise"} == {
        "io-machine",
        "bit-lab",
        "file-workspace",
        "step-machine",
        "network-lab",
    }


def test_all_it_labs_keep_localized_checks_consistent_without_claiming_solved() -> None:
    units = [unit for unit in load_catalogue().units if unit.path_id == "it-foundations" and unit.room != "exercise"]
    assert len(units) == 26
    for unit in units:
        assert unit.completion is not None and unit.exercise is None
        expected = unit.completion.answer
        assert expected
        options_by_locale = {}
        for locale in ("de", "en"):
            content = unit.content[locale]
            assert content["schema"] == "it-lab/1" and content["family"] == unit.room
            assert content["scenario"] == unit.id.removeprefix("itf-")
            assert all(content[field].strip() for field in ("intro", "task", "observe", "worldNote"))
            assert {check["id"]: check["answer"] for check in content["checks"]} == expected
            options_by_locale[locale] = {
                check["id"]: [option["id"] for option in check["options"]] for check in content["checks"]
            }
            assert all(check["answer"] in options_by_locale[locale][check["id"]] for check in content["checks"])
        assert options_by_locale["de"] == options_by_locale["en"]
        completion = Complete(request_id=uuid4(), expected_revision=0, action="complete", answer=expected)
        result = completed_progress(unit, Progress(), completion, solved=False)
        assert result.status == "completed" and result.result is not None and result.result.kind == "introduced"
        check_id = next(iter(expected))
        wrong = {**expected, check_id: "not-an-answer"}
        with pytest.raises(HTTPException) as error:
            completed_progress(unit, Progress(), completion.copy(update={"answer": wrong}), solved=True)
        assert error.value.status_code == 422
        skip = completion.copy(update={"action": "skip", "answer": {}})
        if unit.completion.allow_skip:
            skipped = completed_progress(unit, Progress(), skip, solved=False)
            assert skipped.status == "skipped" and skipped.result is None
        else:
            with pytest.raises(HTTPException) as error:
                completed_progress(unit, Progress(), skip, solved=False)
            assert error.value.status_code == 403


def test_it_assessments_keep_solutions_private_and_require_verified_task_references() -> None:
    units = [unit for unit in load_catalogue().units if unit.path_id == "it-foundations" and unit.room == "exercise"]
    assert len(units) == 24
    for unit in units:
        assert unit.completion is None and unit.exercise is None
        for locale in ("de", "en"):
            content = unit.content[locale]
            allowed = (
                {"question", "answers", "hints"} if "answers" in content else {"question", "left", "right", "hints"}
            )
            assert set(content) == allowed
            assert content["question"].strip() and content["hints"]
            assert all(isinstance(item, str) for item in content.get("answers", []))
        # A client assertion cannot complete an exercise whose real task has not
        # been bound through the operator's verified import configuration.
        request = Complete(request_id=uuid4(), expected_revision=0, action="complete", answer={"solved": True})
        with pytest.raises(HTTPException) as error:
            completed_progress(unit, Progress(), request, solved=True)
        assert error.value.status_code == 422
