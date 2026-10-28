"""The learner's own learning plan ("Dein Lernweg") and the suggestions for it."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool

from api.schemas import BaseModel
from api.schemas.course import Level
from api.schemas.rooms import Mutation
from api.schemas.tracks import Identifier
from api.utils.docs import example

# Compact UTF-8 JSON of the stored state, the size `JSON.stringify` gives in the browser; larger plans get 413.
PLAN_STATE_LIMIT = 16384

Preference = Literal["breit", "tief"]
CourseId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,255}$")]
Reason = Literal["next_after", "in_track", "new_area", "deepen", "level_fit"]


class CourseStepState(BaseModel):
    type: Literal["course"]
    course: CourseId
    # The track and stage the course was taken from, both or neither.
    track: Identifier | None = None
    stage: Identifier | None = None


class ExternalStepState(BaseModel):
    type: Literal["external"]
    track: Identifier
    stage: Identifier
    id: Identifier
    done: StrictBool = False


StepState = Annotated[CourseStepState | ExternalStepState, Field(discriminator="type")]


class PlanState(BaseModel):
    character: Level | None = None
    preference: Preference | None = None
    steps: list[StepState] = Field(default_factory=list)


class SavePlan(Mutation):
    state: PlanState

    model_config = example(
        request_id="0b6c9a62-55c7-4bd4-9f1e-7d3b8c2a1e40",
        expected_revision=3,
        state={
            "character": "einsteiger",
            "preference": "breit",
            "steps": [
                {"type": "course", "course": "docker"},
                {
                    "type": "external",
                    "track": "cloud-engineer",
                    "stage": "practitioner",
                    "id": "workshop-practitioner",
                    "done": True,
                },
            ],
        },
    )


class PlanCourseStep(BaseModel):
    type: Literal["course"] = "course"
    course: str = Field(description="ID of the course")
    title: str = Field(description="Title of the course in the language of the request")
    track: str | None = Field(description="Track the course was taken from, if any")
    stage: str | None = Field(description="Stage of that track, if any")
    status: Literal["done", "in_progress", "open"] = Field(description="By the completion rules of the course")
    done_units: int | None = Field(description="Finished lessons, rooms or lectures; null if not countable now")
    total_units: int | None = Field(description="All lessons, rooms or lectures; null if not countable now")


class PlanExternalStep(BaseModel):
    type: Literal["external"] = "external"
    id: str = Field(description="ID of the external step within its stage")
    title: str = Field(description="Title of the external step in the language of the request")
    url: str | None = Field(description="Where the external step is offered, if known")
    track: str = Field(description="Track of the external step")
    stage: str = Field(description="Stage of the external step")
    status: Literal["done", "open"] = Field(description="Done when the learner confirmed it")
    self_confirmed: bool = Field(description="True when the status rests on the learner's own confirmation")


class PlanNextCourse(BaseModel):
    type: Literal["course"] = "course"
    course: str


class PlanNextExternal(BaseModel):
    type: Literal["external"] = "external"
    id: str
    track: str
    stage: str


class Plan(BaseModel):
    """The learner's plan; revision 0 and no steps before the first save."""

    revision: int = Field(description="Revision to send as `expected_revision` with the next change")
    character: Level | None = Field(description="Self-chosen difficulty")
    preference: Preference | None = Field(description="Self-chosen preference: broad and connected, or deep")
    steps: list[Annotated[PlanCourseStep | PlanExternalStep, Field(discriminator="type")]]
    next: Annotated[PlanNextCourse | PlanNextExternal, Field(discriminator="type")] | None = Field(
        description="The first step that is not done"
    )

    model_config = example(
        revision=3,
        character="einsteiger",
        preference="breit",
        steps=[
            {
                "type": "course",
                "course": "docker",
                "title": "Docker",
                "track": None,
                "stage": None,
                "status": "in_progress",
                "done_units": 4,
                "total_units": 12,
            },
            {
                "type": "external",
                "id": "workshop-practitioner",
                "title": "Workshop Practitioner",
                "url": None,
                "track": "cloud-engineer",
                "stage": "practitioner",
                "status": "done",
                "self_confirmed": True,
            },
        ],
        next={"type": "course", "course": "docker"},
    )


class Suggestion(BaseModel):
    type: Literal["course"] = "course"
    course: str = Field(description="ID of the suggested course")
    title: str = Field(description="Title of the course in the language of the request")
    reason: Reason = Field(description="Text key of the reason; the texts are in the frontend")
    reason_course: str | None = Field(default=None, description="With next_after: the course this one follows")
    reason_track: str | None = Field(default=None, description="With in_track: the track of the plan it belongs to")
