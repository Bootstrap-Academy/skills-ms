"""Curated tracks over courses, loaded once from `config/tracks` and an optional private directory.

A track is content without any personal data. The status of its items for a learner comes from the completion
rules of the courses (`course_progress`) and, for external steps, from the learner's own plan.
"""

from typing import Mapping

from yaml import YAMLError, safe_load

from api.logger import get_logger
from api.schemas.course import Course
from api.schemas.tracks import (
    CourseItemDefinition,
    Language,
    Provider,
    Track,
    TrackCourseItem,
    TrackDefinition,
    TrackExternalItem,
    TrackStage,
    UserTrack,
    UserTrackCourseItem,
    UserTrackExternalItem,
    UserTrackStage,
)
from api.services.course_progress import CourseProgress
from api.services.courses import COURSES
from api.settings import settings

logger = get_logger(__name__)


def request_language(accept_language: str | None) -> Language:
    """The first of de and en in the order of `Accept-Language` (weights respected); German otherwise."""
    ranked: list[tuple[float, int, str]] = []
    for position, entry in enumerate((accept_language or "").split(",")):
        tag, *parameters = (part.strip() for part in entry.split(";"))
        weight = 1.0
        for parameter in parameters:
            name, _, value = parameter.partition("=")
            if name.strip().lower() == "q":
                try:
                    weight = float(value)
                except ValueError:
                    weight = 0.0
        if weight > 0:
            ranked.append((-weight, position, tag.lower().split("-")[0]))
    for _, _, language in sorted(ranked):
        if language == "de":
            return "de"
        if language == "en":
            return "en"
    return "de"


def course_title(course: Course, language: Language) -> str:
    translation = course.translations.get(language)
    return translation.title if translation is not None and translation.title else course.title


def _load_tracks() -> dict[str, TrackDefinition]:
    tracks = {}
    directories = [settings.tracks]
    if private := settings.private_tracks_directory:
        if not private.is_absolute() or not private.is_dir() or private.is_symlink():
            raise ValueError("The configured private track directory is unavailable")
        directories.append(private)
    for directory in directories:
        for file in sorted(directory.glob("*.yml")):
            if directory == settings.private_tracks_directory and (not file.is_file() or file.is_symlink()):
                raise ValueError("Private track definitions must be regular files")
            _id = file.name.removesuffix(".yml")
            logger.debug(f"loading track {_id}")
            with file.open() as f:
                try:
                    definition = safe_load(f)
                    if not isinstance(definition, dict) or ("id" in definition and definition["id"] != _id):
                        raise ValueError("Track ID must match its filename")
                    tracks[_id] = TrackDefinition.model_validate({**definition, "id": _id})
                except (ValueError, TypeError, YAMLError):
                    # Like courses: validation errors may quote private content.
                    raise ValueError(f"Invalid track definition: {_id}") from None
    return tracks


def _check_track_definitions() -> None:
    for track in TRACKS.values():
        for course_id in track.course_ids():
            if course_id not in COURSES:
                # The track stays available; the answers leave the unknown course out.
                logger.warning(f"Unknown course {course_id} in track {track.id}")


TRACKS: dict[str, TrackDefinition] = _load_tracks()
_check_track_definitions()


def public_track(track: TrackDefinition, language: Language) -> Track:
    return Track(
        id=track.id,
        title=track.title.text(language),
        description=None if track.description is None else track.description.text(language),
        provider=None if track.provider is None else Provider(name=track.provider.name, url=track.provider.url),
        level=track.level,
        stages=[
            TrackStage(
                id=stage.id,
                title=stage.title.text(language),
                kind=stage.kind,
                repeat_after_months=stage.repeat_after_months,
                items=[
                    (
                        TrackCourseItem(course=item.course, title=course_title(COURSES[item.course], language))
                        if isinstance(item, CourseItemDefinition)
                        else TrackExternalItem(
                            id=item.external.id, title=item.external.title.text(language), url=item.external.url
                        )
                    )
                    for item in stage.items
                    if not isinstance(item, CourseItemDefinition) or item.course in COURSES
                ],
            )
            for stage in track.stages
        ],
    )


def list_tracks(language: Language) -> list[Track]:
    return [public_track(track, language) for track in sorted(TRACKS.values(), key=lambda track: track.id)]


def user_track(
    track: TrackDefinition,
    language: Language,
    progress: Mapping[str, CourseProgress],
    confirmed: set[tuple[str, str, str]],
) -> UserTrack:
    """The track with a status per item: courses by their completion rules, external steps by the plan."""
    public = public_track(track, language)
    return UserTrack(
        **public.model_dump(exclude={"stages"}),
        stages=[
            UserTrackStage(
                **stage.model_dump(exclude={"items"}),
                items=[
                    (
                        UserTrackCourseItem(
                            **item.model_dump(),
                            status=progress[item.course].status if item.course in progress else "open",
                        )
                        if isinstance(item, TrackCourseItem)
                        else UserTrackExternalItem(
                            **item.model_dump(), status="done" if (track.id, stage.id, item.id) in confirmed else "open"
                        )
                    )
                    for item in stage.items
                ],
            )
            for stage in public.stages
        ],
    )
