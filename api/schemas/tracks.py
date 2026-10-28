"""Curated tracks over courses ("Lernpfade"): their definitions in `config/tracks/*.yml` and their answers."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from api.schemas import BaseModel
from api.schemas.course import Level
from api.utils.docs import example

# The languages a track is answered in; German is the default and the fallback.
Language = Literal["de", "en"]
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")]
Url = Annotated[str, StringConstraints(pattern=r"^https://[^\s]+$", max_length=2048)]
ItemStatus = Literal["done", "in_progress", "open"]


class LocalizedDefinition(BaseModel):
    """A text in German and, optionally, English; a plain string is the German text."""

    de: str = Field(min_length=1)
    en: str | None = Field(default=None, min_length=1)

    @model_validator(mode="before")
    @classmethod
    def plain_text(cls, value: Any) -> Any:
        return {"de": value} if isinstance(value, str) else value

    def text(self, language: Language) -> str:
        return self.en if language == "en" and self.en else self.de


class ProviderDefinition(BaseModel):
    name: str = Field(min_length=1)
    url: Url | None = None


class ExternalDefinition(BaseModel):
    id: Identifier
    title: LocalizedDefinition
    url: Url | None = None


class CourseItemDefinition(BaseModel):
    course: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,255}$")

    @property
    def key(self) -> tuple[str, str]:
        return "course", self.course


class ExternalItemDefinition(BaseModel):
    external: ExternalDefinition

    @property
    def key(self) -> tuple[str, str]:
        return "external", self.external.id


class StageDefinition(BaseModel):
    id: Identifier
    title: LocalizedDefinition
    # A free label; fundamentals, practitioner, mastery, refresher and certification get symbols in the frontend.
    kind: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    repeat_after_months: int | None = Field(default=None, ge=1, le=120)
    items: list[CourseItemDefinition | ExternalItemDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        keys = [item.key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("Every course and external step appears once per stage")
        return self


class TrackDefinition(BaseModel):
    id: Identifier
    title: LocalizedDefinition
    description: LocalizedDefinition | None = None
    provider: ProviderDefinition | None = None
    level: Level
    stages: list[StageDefinition] = Field(min_length=1)

    @field_validator("stages")
    @classmethod
    def unique_stages(cls, stages: list[StageDefinition]) -> list[StageDefinition]:
        if len({stage.id for stage in stages}) != len(stages):
            raise ValueError("Every stage id appears once per track")
        return stages

    def stage(self, stage_id: str | None) -> StageDefinition | None:
        return next((stage for stage in self.stages if stage.id == stage_id), None)

    def course_ids(self) -> list[str]:
        """The courses of the track in their order, each once."""
        found = (item.course for stage in self.stages for item in stage.items if isinstance(item, CourseItemDefinition))
        return list(dict.fromkeys(found))


class Provider(BaseModel):
    name: str = Field(description="Name of the provider of the track")
    url: str | None = Field(description="Web page of the provider")


class TrackCourseItem(BaseModel):
    type: Literal["course"] = Field(default="course", description="A course of this service")
    course: str = Field(description="ID of the course")
    title: str = Field(description="Title of the course in the language of the request")


class TrackExternalItem(BaseModel):
    type: Literal["external"] = Field(default="external", description="An offer outside this service")
    id: str = Field(description="ID of the external step, unique within its stage")
    title: str = Field(description="Title of the external step in the language of the request")
    url: str | None = Field(description="Where the external step is offered, if known")


class TrackStage(BaseModel):
    id: str = Field(description="ID of the stage, unique within its track")
    title: str = Field(description="Title of the stage in the language of the request")
    kind: str = Field(description="Kind of the stage; fundamentals, practitioner, mastery, refresher, certification")
    repeat_after_months: int | None = Field(description="Suggested interval for repeating the stage, if any")
    items: list[Annotated[TrackCourseItem | TrackExternalItem, Field(discriminator="type")]]


class Track(BaseModel):
    id: str = Field(description="ID of the track")
    title: str = Field(description="Title of the track in the language of the request")
    description: str | None = Field(description="Description of the track in the language of the request")
    provider: Provider | None = Field(description="Who offers the track; null for tracks of this academy")
    level: Level = Field(description="Difficulty the track starts at")
    stages: list[TrackStage]

    model_config = example(
        id="cloud-engineer",
        title="Cloud Engineer",
        description="Vom ersten Server bis zur eigenen Cloud-Umgebung.",
        provider=None,
        level="einsteiger",
        stages=[
            {
                "id": "fundamentals",
                "title": "Fundamentals",
                "kind": "fundamentals",
                "repeat_after_months": None,
                "items": [
                    {"type": "course", "course": "cloud", "title": "Cloud Technologien"},
                    {"type": "external", "id": "workshop-practitioner", "title": "Workshop Practitioner", "url": None},
                ],
            }
        ],
    )


class UserTrackCourseItem(TrackCourseItem):
    status: ItemStatus = Field(description="Progress of the learner in the course, by the course's completion rules")


class UserTrackExternalItem(TrackExternalItem):
    status: Literal["done", "open"] = Field(description="Done when the learner confirmed it in the learning plan")


class UserTrackStage(TrackStage):
    items: list[  # type: ignore[assignment]
        Annotated[UserTrackCourseItem | UserTrackExternalItem, Field(discriminator="type")]
    ]


class UserTrack(Track):
    """A track as a signed-in learner sees it: every item with its status."""

    stages: list[UserTrackStage]  # type: ignore[assignment]
