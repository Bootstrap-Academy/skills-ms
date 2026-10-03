"""Private authoring inputs fail closed without changing the pinned base catalogue."""

import json
import logging
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from yaml import safe_dump

from api.logger import AssetGrantFilter, redact_sentry_event
from api.services import courses, rooms
from api.settings import settings
from tests.endpoints.test_curriculum import composed, course_definition


def test_private_course_overlay_preserves_other_courses_and_rejects_invalid_input(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    baseline = courses._load_courses()
    assert len(baseline) == 87
    private = tmp_path / "courses"
    private.mkdir()
    changed = baseline["it-foundations"].dict(exclude={"id"})
    changed["title"] = "Synthetic private title"
    (private / "it-foundations.yml").write_text(safe_dump(changed))
    (private / "synthetic-added.yml").write_text(safe_dump(course_definition(id="synthetic-added").dict()))
    monkeypatch.setattr(settings, "private_courses_directory", private)
    merged = courses._load_courses()
    assert len(merged) == 88 and merged["it-foundations"].price == baseline["it-foundations"].price == 0
    assert merged["it-foundations"].title == "Synthetic private title"
    assert all(merged[cid] == value for cid, value in baseline.items() if cid != "it-foundations")
    metadata = (
        course_definition(
            description="Public course description",
            curriculum=composed("PRIVATE-UNIT"),
            sections=[
                {
                    "id": "video",
                    "title": "Public section title",
                    "description": "PRIVATE-SECTION-TEXT",
                    "lectures": [
                        {
                            "id": "video",
                            "title": "Public lecture title",
                            "description": "PRIVATE-LECTURE-TEXT",
                            "type": "youtube",
                            "video_id": "PRIVATE-VIDEO-ID",
                            "duration": 10,
                        }
                    ],
                }
            ],
        )
        .summary(None)
        .json()
    )
    assert "Public course description" in metadata
    assert not any(
        value in metadata
        for value in (
            "PRIVATE-UNIT",
            "PRIVATE-SECTION-TEXT",
            "PRIVATE-LECTURE-TEXT",
            "PRIVATE-VIDEO-ID",
            '"curriculum"',
        )
    )
    (private / "it-foundations.yml").write_text("price: invalid\ndescription: PRIVATE-TEXT")
    with pytest.raises(ValueError) as error:
        courses._load_courses()
    assert "PRIVATE-TEXT" not in str(error.value)
    (private / "it-foundations.yml").write_text("description: [PRIVATE-TEXT")
    with pytest.raises(ValueError) as error:
        courses._load_courses()
    assert "PRIVATE-TEXT" not in str(error.value)
    monkeypatch.setattr(settings, "private_courses_directory", tmp_path / "missing")
    with pytest.raises(ValueError, match="unavailable"):
        courses._load_courses()


def test_optional_private_room_catalogue_never_falls_back_on_error(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    rooms.load_catalogue.cache_clear()
    baseline = rooms.load_catalogue()
    private = tmp_path / "learning_rooms.json"
    private.write_text(baseline.json())
    monkeypatch.setattr(settings, "learning_rooms_content", private)
    try:
        rooms.load_catalogue.cache_clear()
        assert rooms.load_catalogue() == baseline
        for value in ('{"units": "PRIVATE-TEXT"}', "invalid json"):
            private.write_text(value)
            rooms.load_catalogue.cache_clear()
            with pytest.raises(HTTPException) as error:
                rooms.load_catalogue()
            assert error.value.status_code == 503 and "PRIVATE-TEXT" not in str(error.value.detail)
        private.unlink()
        rooms.load_catalogue.cache_clear()
        with pytest.raises(HTTPException):
            rooms.load_catalogue()
        target = tmp_path / "real.json"
        target.write_text(baseline.json())
        private.symlink_to(target)
        with pytest.raises(HTTPException):
            rooms.load_catalogue()
    finally:
        rooms.load_catalogue.cache_clear()


def test_logs_and_sentry_never_contain_asset_grants() -> None:
    token = "a" * 43
    path = f"/skills/lesson-assets/{token}/{'b' * 64}/entry.mjs"
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 1, '%s - "%s %s HTTP/%s" %s', ("127.0.0.1", "GET", path, "1.1", 200), None
    )
    assert AssetGrantFilter().filter(record)
    assert token not in record.getMessage() and "[redacted]" in record.getMessage()
    event = {
        "request": {"url": "https://api.example" + path, "path_params": {"grant": token}},
        "breadcrumbs": [{"message": path}],
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "module": "api.services.private_lesson_modules",
                                "vars": {"token": token, "digest": "RAW-DIGEST"},
                            },
                            {"filename": "/app/api/endpoints/lesson_assets.py", "vars": {"grant": token}},
                            {"module": "other", "vars": {"request": {"path_params": {"grant": token}}, "token": token}},
                        ]
                    }
                }
            ]
        },
    }
    assert token not in json.dumps(redact_sentry_event(event, {}))
    assert "RAW-DIGEST" not in json.dumps(redact_sentry_event(event, {}))
