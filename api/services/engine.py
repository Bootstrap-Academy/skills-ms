"""Package revisions of the lesson engine: publishing the server side of a unit, and the descriptor of its package.

An operator publishes a revision with the admin route; the newest published revision of a unit is its active one,
and publishing the same content again makes that revision active again. The row of the unit's lesson module in
the registry is the lock for every change of the active revision. Its API version stays 1: the engine's package
descriptor (API 2) is derived from the active revision and its hashes, never from that row, so the registry needs
no migration; the row is the anchor of the foreign key and of the lock.
"""

from contextvars import ContextVar
from hashlib import sha256
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError

from api.database import db, filter_by, select
from api.logger import get_logger
from api.models import EngineRevision, LessonModule, SubSkill
from api.schemas.engine import RevisionPublished, RevisionRequest, ServerDocument
from api.schemas.lesson_module import EnginePackageDescriptor
from api.schemas.rooms import CatalogueUnit, EngineCompletion
from api.settings import settings
from api.utils.canonical import CanonicalError, canonical
from api.utils.utc import utcnow

logger = get_logger(__name__)

# The template's document has 143 KB; ten parts of the sandbox and their metadata stay well below.
MAX_DOCUMENT_BYTES = 1024 * 1024
UNAVAILABLE = "Diese Lektion kann gerade nicht geladen werden. Versuch es bitte noch einmal."


# The units of the engine with an active revision, read once per request (`load_active_units`) so that the
# synchronous catalogue can treat every other unit of the engine like a retired one (Ä38 b).
_ACTIVE_UNITS: ContextVar[frozenset[str]] = ContextVar("engine_active_units", default=frozenset())


def is_engine_unit(unit: CatalogueUnit) -> bool:
    return isinstance(unit.completion, EngineCompletion)


async def load_active_units() -> None:
    """Remember for this request which units of the engine learners can open: switched on, with a content origin
    and an active revision. Without the switch nothing is read and no unit of the engine is available."""
    if not settings.engine_enabled or settings.engine_content_origin is None:
        _ACTIVE_UNITS.set(frozenset())
        return
    rows = await db.all(filter_by(EngineRevision).where(EngineRevision.active_for.is_not(None)))
    _ACTIVE_UNITS.set(frozenset(row.unit_id for row in rows))


def available(unit: CatalogueUnit) -> bool:
    return unit.id in _ACTIVE_UNITS.get()


# K5 (Ä36): a frame may flood the saves of its unit; the service takes at most this many per learner and unit.
SAVE_WINDOW_SECONDS = 30
SAVES_PER_WINDOW = 60


async def count_save(key: str) -> int:
    from api.redis import redis

    # One transaction: the first save of a window starts the window, every save counts.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(key, 0, ex=SAVE_WINDOW_SECONDS, nx=True)
        pipe.incr(key)
        _, count = await pipe.execute()
    return int(count)


async def allow_save(user_id: str, unit_id: str) -> bool:
    try:
        count = await count_save(f"engine-saves:{user_id}:{unit_id}")
    except (RedisError, OSError):
        # A soft limit: without Redis the save goes through, as before this limit existed.
        logger.warning("engine save limit unavailable")
        return True
    return count <= SAVES_PER_WINDOW


def task_terms(document: ServerDocument) -> list[dict[str, Any]]:
    """What a revision keeps of each task: the server books verdicts and XP against these values."""
    return [
        {
            "goalId": task.goalId,
            "required": task.required,
            "xp": task.xp,
            "subskillId": task.subskillId,
            "input": task.input,
        }
        for task in document.tasks
    ]


def _bound(tasks: list[dict[str, Any]]) -> dict[str, tuple[bool, int, str, str]]:
    # The input kind belongs to it: a saved plan of one kind fits no task of the other (Ä38 c).
    return {task["goalId"]: (task["required"], task["xp"], task["subskillId"], task["input"]) for task in tasks}


def _refuse(status: int, detail: Any) -> HTTPException:
    return HTTPException(status, detail)


def checked_document(data: RevisionRequest, unit: CatalogueUnit) -> ServerDocument:
    try:
        text = canonical(data.server_document)
    except CanonicalError as error:
        raise _refuse(422, f"server_document has no canonical form: {error}") from None
    if len(text) > MAX_DOCUMENT_BYTES:
        raise _refuse(413, f"server_document has {len(text)} bytes; at most {MAX_DOCUMENT_BYTES} are accepted")
    if sha256(text).hexdigest() != data.server_sha256:
        raise _refuse(422, "server_sha256 is not the SHA-256 of the canonical JSON of server_document")
    try:
        document = ServerDocument.model_validate(data.server_document)
    except ValidationError as error:
        problems = [f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()[:20]]
        raise _refuse(422, {"server_document": problems}) from None
    if document.unitId != unit.id:
        raise _refuse(422, f"server_document belongs to unit {document.unitId}, not to {unit.id}")
    runner = document.runner
    names = [runner.main.name, *(part.name for part in runner.parts)]
    if len(set(names)) != len(names):
        raise _refuse(422, "The files of the runner have unique names")
    bundle = "".join(part.content for part in runner.parts).encode("ascii")
    if sha256(bundle).hexdigest() != runner.sha256:
        raise _refuse(422, "The parts of the runner do not match runner.sha256")
    return document


async def publish(data: RevisionRequest) -> RevisionPublished:
    from api.services import rooms

    # Before its first revision a unit of the engine is hidden from learners, not from its publisher.
    content = rooms.catalogue(unpublished=True)
    unit = next((item for item in content.units if item.id == data.unit_id and not item.retired), None)
    if unit is None or not is_engine_unit(unit) or unit.module_id is None:
        raise _refuse(404, "There is no unit of the lesson engine with this id")
    document = checked_document(data, unit)
    tasks = task_terms(document)

    wanted = {task["subskillId"] for task in tasks}
    known = {row.id for row in await db.all(select(SubSkill).where(SubSkill.id.in_(wanted)))}
    if missing := sorted(wanted - known):
        raise _refuse(422, f"Unknown sub-skills: {', '.join(missing)}")

    # The registry row of the module serializes every change of the active revision of this unit.
    module = await db.first(
        filter_by(LessonModule, id=unit.module_id).with_for_update().execution_options(populate_existing=True)
    )
    if module is None:
        raise _refuse(409, f"Register the lesson module {unit.module_id} of the unit first")
    revisions = await db.all(filter_by(EngineRevision, unit_id=unit.id).with_for_update())
    active = next((row for row in revisions if row.active_for is not None), None)
    if active is not None and _bound(active.tasks) != _bound(tasks):
        # §9: a later revision keeps task ids, required tasks, XP amounts and sub-skills, and the input kinds.
        raise _refuse(
            409, "A new revision keeps the task ids, the required tasks, their XP, their sub-skills and their input"
        )
    if active is not None and active.server_document["bandRule"]["bands"] != document.bandRule.bands:
        # A task state may hold any band of the active revision (Ä38 c).
        raise _refuse(409, "A new revision keeps the bands of the active one")

    same = next(
        (row for row in revisions if row.server_sha256 == data.server_sha256 and row.package_hash == data.package_hash),
        None,
    )
    if same is not None and (same.manifest_hash, same.entry_path, same.evidence_sha256) != (
        data.manifest_hash,
        data.entry_path,
        data.evidence_sha256,
    ):
        raise _refuse(409, "This content was published with another manifest, entry or evidence")

    if active is not None and active is not same:
        active.active_for = None
        await db.session.flush()
    if same is not None:
        same.active_for = unit.id
        await db.session.flush()
        return RevisionPublished(revision_id=same.id, number=same.number, active=True, created=False)

    row = EngineRevision(
        id=str(uuid4()),
        unit_id=unit.id,
        number=max((item.number for item in revisions), default=0) + 1,
        active_for=unit.id,
        module_id=unit.module_id,
        package_hash=data.package_hash,
        manifest_hash=data.manifest_hash,
        entry_path=data.entry_path,
        server_document=data.server_document,
        server_sha256=data.server_sha256,
        evidence_sha256=data.evidence_sha256,
        tasks=tasks,
        created_at=utcnow(),
    )
    await db.add(row)
    try:
        await db.session.flush()
    except IntegrityError:
        await db.session.rollback()
        raise _refuse(409, "Another revision of this unit was published at the same time") from None
    return RevisionPublished(revision_id=row.id, number=row.number, active=True, created=True)


async def descriptor(unit: CatalogueUnit) -> EnginePackageDescriptor:
    """The package of the unit's active revision, at the content origin the frontend loads it from.

    The catalogue offers a unit of the engine only with both (Ä38 b); a revision deactivated during this very
    request is the one case left for the 503."""
    origin = settings.engine_content_origin
    row = await db.first(filter_by(EngineRevision, active_for=unit.id))
    if origin is None or row is None:
        raise HTTPException(503, UNAVAILABLE)
    base = f"{origin}/packages/{row.package_hash}/"
    return EnginePackageDescriptor(
        id=row.module_id,
        api_version=2,
        entry_url=base + row.entry_path,
        manifest_url=base + "manifest.json",
        manifest_hash=row.manifest_hash,
        package_hash=row.package_hash,
        revision_id=row.id,
    )
