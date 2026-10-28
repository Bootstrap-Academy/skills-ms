"""Curated tracks (`config/tracks`): their content, their schema, the private directory and the public answers."""

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from pytest import MonkeyPatch
from yaml import safe_dump

from api.schemas.tracks import TrackDefinition
from api.services import tracks
from api.services.courses import COURSES
from api.settings import settings


def definition(**values: Any) -> dict[str, Any]:
    return {
        "title": {"de": "Synthetischer Pfad", "en": "Synthetic track"},
        "level": "einsteiger",
        "stages": [
            {"id": "fundamentals", "title": "Grundlagen", "kind": "fundamentals", "items": [{"course": "linux"}]}
        ],
        **values,
    }


def test_the_repository_holds_three_tracks_of_existing_courses() -> None:
    assert sorted(tracks.TRACKS) == ["cloud-engineer", "einstieg-it", "web-entwicklung"]
    for track in tracks.TRACKS.values():
        assert track.course_ids() and set(track.course_ids()) <= set(COURSES), track.id
        # The tracks are the academy's own; no partner is named in them.
        assert track.provider is None
    assert tracks.TRACKS["einstieg-it"].course_ids() == [
        "it-foundations",
        "medienkompetenz",
        "linux",
        "netzwerktechnik",
    ]
    cloud = tracks.TRACKS["cloud-engineer"]
    assert [stage.kind for stage in cloud.stages] == [
        "fundamentals",
        "practitioner",
        "mastery",
        "refresher",
        "certification",
    ]
    assert cloud.course_ids() == ["cloud", "linux", "netzwerktechnik", "docker", "iam"]
    assert cloud.stage("refresher") is not None and cloud.stage("refresher").repeat_after_months == 18  # type: ignore
    assert cloud.stage("practitioner").items[0].external.id == "workshop-practitioner"  # type: ignore


def test_about_ten_courses_carry_a_level() -> None:
    levelled = {course.id: course.level for course in COURSES.values() if course.level is not None}
    assert 10 <= len(levelled) <= 14
    assert levelled["linux"] == "einsteiger" and levelled["docker"] == "fortgeschritten"


@pytest.mark.parametrize(
    "header,language",
    [
        (None, "de"),
        ("", "de"),
        ("en", "en"),
        ("en-US,en;q=0.9", "en"),
        ("fr-FR, en;q=0.5, de;q=0.8", "de"),
        ("de;q=0, en", "en"),
        ("en;q=oops, de", "de"),
        ("*", "de"),
        ("fr", "de"),
    ],
)
def test_the_language_follows_accept_language(header: str | None, language: str) -> None:
    assert tracks.request_language(header) == language


@pytest.mark.parametrize(
    "change",
    [
        {"level": "anfänger"},
        {"stages": []},
        {"title": None},
        {"stages": [{"id": "a", "title": "A", "kind": "fundamentals"}, {"id": "a", "title": "B", "kind": "mastery"}]},
        {"stages": [{"id": "a", "title": "A", "kind": "x", "items": [{"course": "linux"}, {"course": "linux"}]}]},
        {
            "stages": [
                {
                    "id": "a",
                    "title": "A",
                    "kind": "x",
                    "items": [{"external": {"id": "e", "title": "E", "url": "ftp://x"}}],
                }
            ]
        },
        {"stages": [{"id": "a", "title": "A", "kind": "x", "repeat_after_months": 0}]},
        {"provider": {"name": "", "url": None}},
        {"unknown": True},
        {"id": "another-id"},
    ],
)
def test_an_invalid_definition_fails_without_quoting_it(
    tmp_path: Path, monkeypatch: MonkeyPatch, change: dict[str, Any]
) -> None:
    private = tmp_path / "tracks"
    private.mkdir()
    (private / "synthetic.yml").write_text(safe_dump(definition(description="PRIVATE-TRACK-TEXT", **change)))
    monkeypatch.setattr(settings, "private_tracks_directory", private)
    with pytest.raises(ValueError) as error:
        tracks._load_tracks()
    assert str(error.value) == "Invalid track definition: synthetic"
    assert error.value.__cause__ is None and error.value.__suppress_context__


def test_the_private_directory_adds_and_replaces_tracks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    private = tmp_path / "tracks"
    private.mkdir()
    (private / "einstieg-it.yml").write_text(safe_dump(definition(title="Ersetzt")))
    (private / "synthetic.yml").write_text(safe_dump(definition()))
    monkeypatch.setattr(settings, "private_tracks_directory", private)
    merged = tracks._load_tracks()
    assert sorted(merged) == ["cloud-engineer", "einstieg-it", "synthetic", "web-entwicklung"]
    assert merged["einstieg-it"].title.text("en") == "Ersetzt"
    assert merged["web-entwicklung"] == tracks.TRACKS["web-entwicklung"]


@pytest.mark.parametrize("kind", ["missing", "relative", "symlink"])
def test_an_unusable_private_directory_fails_closed(tmp_path: Path, monkeypatch: MonkeyPatch, kind: str) -> None:
    target = tmp_path / "tracks"
    target.mkdir()
    paths = {"missing": tmp_path / "missing", "relative": Path("tracks"), "symlink": tmp_path / "link"}
    if kind == "symlink":
        paths["symlink"].symlink_to(target)
    monkeypatch.setattr(settings, "private_tracks_directory", paths[kind])
    with pytest.raises(ValueError, match="private track directory"):
        tracks._load_tracks()


def test_an_unknown_course_is_left_out_of_the_answer() -> None:
    track = TrackDefinition.model_validate(
        definition(
            id="synthetic",
            stages=[
                {
                    "id": "fundamentals",
                    "title": {"de": "Grundlagen", "en": "Fundamentals"},
                    "kind": "fundamentals",
                    "items": [
                        {"course": "missing-course"},
                        {"course": "it-foundations"},
                        {"external": {"id": "workshop", "title": {"de": "Workshop"}}},
                    ],
                }
            ],
        )
    )
    answer = tracks.public_track(track, "en").model_dump(mode="json")
    assert answer["stages"][0] == {
        "id": "fundamentals",
        "title": "Fundamentals",
        "kind": "fundamentals",
        "repeat_after_months": None,
        "items": [
            {"type": "course", "course": "it-foundations", "title": "IT foundations: understand computers"},
            # Without an English title the German one is used.
            {"type": "external", "id": "workshop", "title": "Workshop", "url": None},
        ],
    }
    assert track.course_ids() == ["missing-course", "it-foundations"]


async def test_the_list_of_tracks_is_public_cacheable_and_localized(client: AsyncClient) -> None:
    german = await client.get("/tracks")
    assert german.status_code == 200
    assert german.headers["Cache-Control"] == "public, max-age=300" and german.headers["Vary"] == "Accept-Language"
    assert [track["id"] for track in german.json()] == ["cloud-engineer", "einstieg-it", "web-entwicklung"]
    cloud = german.json()[0]
    assert (cloud["title"], cloud["provider"], cloud["level"]) == ("Cloud Engineer", None, "einsteiger")
    assert cloud["stages"][0]["items"][0] == {"type": "course", "course": "cloud", "title": "Cloud Technologien"}
    assert cloud["stages"][1]["items"] == [
        {"type": "external", "id": "workshop-practitioner", "title": "Workshop Practitioner", "url": None}
    ]
    assert cloud["stages"][3]["title"] == "Auffrischung" and cloud["stages"][3]["repeat_after_months"] == 18
    english = await client.get("/tracks", headers={"Accept-Language": "en-GB,en;q=0.9"})
    assert english.json()[0]["stages"][3]["title"] == "Refresher"
    assert english.json()[1]["title"] == "Getting started in IT"
    assert english.json()[1]["stages"][0]["items"][0]["title"] == "IT foundations: understand computers"
    assert "status" not in german.text


async def test_one_track_for_a_guest_has_no_status(client: AsyncClient) -> None:
    response = await client.get("/tracks/einstieg-it")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "public, max-age=300"
    assert response.headers["Vary"] == "Accept-Language, Authorization"
    assert response.json() == next(
        track for track in (await client.get("/tracks")).json() if track["id"] == "einstieg-it"
    )
    assert "status" not in response.text


async def test_an_unknown_track_is_404(client: AsyncClient) -> None:
    assert (await client.get("/tracks/unknown")).status_code == 404
