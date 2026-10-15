"""Wire formats of the lesson engine: the server document an operator publishes, and the admin route for it.

The server document is written by the engine (`lesson-engine`, `src/build/server-document.ts`, format 1). It is
private: it holds the runner that evaluates submissions in the sandbox, and it reaches no browser. This service
reads from it what it needs to start tasks and book verdicts; it never runs any of it.
"""

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from api.schemas import BaseModel

ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"
HASH_PATTERN = r"^[0-9a-f]{64}$"
# A request id of the lesson protocol (`$defs.id` of protocol 2.0); task states keep up to 100 characters.
OPERATION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,100}$"
OperationId = Annotated[str, Field(pattern=OPERATION_ID_PATTERN)]
Hash = Annotated[str, Field(pattern=HASH_PATTERN)]
EngineId = Annotated[str, Field(pattern=ID_PATTERN)]
# Sandkasten: a program has a main file and at most ten more, each of at most 65,536 characters.
SANDBOX_FILE_NAME = r"^[a-zA-Z0-9._-]{1,32}$"
SANDBOX_FILE_CHARS = 65536
SANDBOX_FILES = 10
SAFE_INTEGER = 2**53 - 1


class EngineModel(BaseModel):
    # The engine names its fields in camelCase; strictness keeps a changed format from passing unnoticed.
    model_config = ConfigDict(extra="forbid", strict=True, coerce_numbers_to_str=False)


class BandRule(EngineModel):
    bands: list[EngineId] = Field(min_length=1, max_length=20)
    initial: EngineId
    lowerAfter: int = Field(ge=2, le=1000)  # noqa: N815
    raiseAfter: int = Field(ge=2, le=1000)  # noqa: N815

    @model_validator(mode="after")
    def initial_band(self) -> "BandRule":
        if len(set(self.bands)) != len(self.bands) or self.initial not in self.bands:
            raise ValueError("bands are unique and contain the initial band")
        return self


class Practice(EngineModel):
    caseId: EngineId  # noqa: N815
    config: dict[str, Any]
    initial: Any


class DocumentTask(EngineModel):
    goalId: EngineId  # noqa: N815
    subskillId: str = Field(min_length=1, max_length=256)  # noqa: N815
    concepts: list[str]
    xp: int = Field(ge=0, le=SAFE_INTEGER)
    required: bool
    input: Literal["program", "actions"]
    criteria: list[str] = Field(min_length=1)
    feedback: dict[str, dict[str, str]]
    practice: dict[str, Practice]


class SandboxFile(EngineModel):
    name: str = Field(pattern=SANDBOX_FILE_NAME)
    content: str = Field(max_length=SANDBOX_FILE_CHARS)


class Runner(EngineModel):
    main: SandboxFile
    parts: list[SandboxFile] = Field(min_length=1, max_length=SANDBOX_FILES)
    sha256: Hash

    @field_validator("parts")
    @classmethod
    def ascii_parts(cls, parts: list[SandboxFile]) -> list[SandboxFile]:
        if not all(part.content.isascii() for part in parts):
            raise ValueError("The parts of the server bundle are ASCII")
        return parts


class Kernel(EngineModel):
    id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)


class ServerDocument(EngineModel):
    formatVersion: Literal[1]  # noqa: N815
    engine: str = Field(min_length=1, max_length=40)
    unitId: EngineId  # noqa: N815
    kernel: Kernel
    bandRule: BandRule  # noqa: N815
    tasks: list[DocumentTask] = Field(min_length=1, max_length=20)
    runner: Runner

    @model_validator(mode="after")
    def tasks_cover_bands(self) -> "ServerDocument":
        goals = [task.goalId for task in self.tasks]
        if len(set(goals)) != len(goals):
            raise ValueError("goal ids are unique")
        if not any(task.required for task in self.tasks):
            raise ValueError("a unit has at least one required task")
        for task in self.tasks:
            if set(task.practice) != set(self.bandRule.bands):
                raise ValueError(f"task {task.goalId} has a practice case in every band and no other")
        return self


class RevisionRequest(BaseModel):
    """An operator publishes the server side of a package: its browser bytes are installed separately (step 5)."""

    unit_id: EngineId
    package_hash: Hash
    manifest_hash: Hash
    entry_path: str = Field(pattern=r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", max_length=512)
    evidence_sha256: Hash
    # SHA-256 of the canonical JSON of `server_document`, as the engine computes it (`documentHash`).
    server_sha256: Hash
    server_document: dict[str, Any]

    @field_validator("entry_path")
    @classmethod
    def inside_package(cls, value: str) -> str:
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("The entry lies inside the package")
        return value


class RevisionPublished(BaseModel):
    revision_id: str
    number: int
    active: bool
    created: bool


# --- Start and submission ---------------------------------------------------------------------------------------
# These bodies speak the shapes of the lesson protocol (camelCase), so the host passes them on unchanged.

PLAN_BYTES = 65536


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, coerce_numbers_to_str=False)


class PlanAction(ProtocolModel):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    target: str = Field(min_length=1, max_length=80)
    args: dict[str, Any]
    actor: str = Field(min_length=1, max_length=80)
    tick: int = Field(ge=0, le=2**31 - 1)


class PlanStep(ProtocolModel):
    step: Literal[True]
    tick: int = Field(ge=0, le=2**31 - 1)


class ProgramPlan(ProtocolModel):
    kind: Literal["program"]
    source: str

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        if len(value.encode("utf-8")) > PLAN_BYTES:
            raise ValueError(f"A program has at most {PLAN_BYTES} bytes")
        return value


class ActionsPlan(ProtocolModel):
    kind: Literal["actions"]
    actions: list[PlanAction | PlanStep] = Field(max_length=4096)


# Ä28: the service reads a plan with its own schema, never with the schema of the package.
InputPlan = Annotated[ProgramPlan | ActionsPlan, Field(discriminator="kind")]


class StartRequest(ProtocolModel):
    requestId: OperationId  # noqa: N815
    revisionId: str = Field(pattern=r"^[0-9a-f-]{36}$")  # noqa: N815
    courseId: str | None = Field(default=None, max_length=256)  # noqa: N815


class Answer(ProtocolModel):
    startId: str = Field(pattern=r"^[0-9a-f-]{36}$")  # noqa: N815
    input: InputPlan


class SubmitRequest(ProtocolModel):
    requestId: OperationId  # noqa: N815
    stateRevision: int = Field(ge=0, le=2147483646)  # noqa: N815
    answer: Answer
    courseId: str | None = Field(default=None, max_length=256)  # noqa: N815


class PublicStart(ProtocolModel):
    startId: str  # noqa: N815
    revisionId: str  # noqa: N815
    goalId: str  # noqa: N815
    caseId: str  # noqa: N815
    config: dict[str, Any]
    initial: Any
    seed: str = Field(pattern=r"^[0-9a-f]{32}$")
