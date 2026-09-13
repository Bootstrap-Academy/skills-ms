"""Only operator-approved origins can provide trusted first-party lesson modules."""

from urllib.parse import urlsplit

from fastapi import HTTPException

from api.database import db
from api.models.lesson_module import LessonModule
from api.schemas.lesson_module import LessonModuleDescriptor
from api.settings import settings


def checked_descriptor(descriptor: LessonModuleDescriptor) -> LessonModuleDescriptor:
    parsed = urlsplit(descriptor.entry_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    allowed = set(settings.lesson_module_origins)
    if origin not in allowed:
        raise ValueError("The module origin is not approved")
    if parsed.scheme != "https" and not (
        settings.lesson_module_local_development and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    ):
        raise ValueError("Modules require HTTPS; local HTTP needs explicit development configuration")
    return descriptor


async def resolve_module(module_id: str) -> LessonModuleDescriptor:
    row = await db.get(LessonModule, id=module_id)
    if row is None:
        raise HTTPException(503, "Diese Lektion kann gerade nicht geladen werden. Versuch es bitte noch einmal.")
    try:
        return checked_descriptor(
            LessonModuleDescriptor.parse_obj({"id": row.id, "api_version": row.api_version, "entry_url": row.entry_url})
        )
    except ValueError:
        raise HTTPException(
            503, "Diese Lektion kann gerade nicht geladen werden. Versuch es bitte noch einmal."
        ) from None


async def register_module(descriptor: LessonModuleDescriptor, *, replace: bool = False) -> None:
    descriptor = checked_descriptor(descriptor)
    row = await db.get(LessonModule, id=descriptor.id)
    if row is None:
        await db.add(LessonModule(**descriptor.dict()))
    elif (row.api_version, row.entry_url) != (descriptor.api_version, descriptor.entry_url):
        if not replace:
            raise ValueError("This module ID is already registered; use --replace for a reviewed publication")
        row.api_version, row.entry_url = descriptor.api_version, descriptor.entry_url
    await db.session.flush()
