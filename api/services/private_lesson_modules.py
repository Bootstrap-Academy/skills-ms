"""Course-authorized, expiring access to operator-published private module packages.

The filesystem publisher is the package trust boundary. Neither a browser nor a
module can publish files, choose an arbitrary filesystem path or mint a grant.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import quote, unquote, urlsplit

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from api.database import db
from api.models import PurchaseUser
from api.models.lesson_module import LessonModule
from api.redis import redis
from api.schemas.lesson_module import LessonModuleDescriptor
from api.schemas.user import User
from api.settings import settings


REFERENCE_PREFIX = "/private-lesson-modules/"
ACCEL_PREFIX = "/_private-lesson-modules/"
HASH = re.compile(r"[0-9a-f]{64}\Z")
GRANT = re.compile(r"[A-Za-z0-9_-]{43}\Z")
MAX_MANIFEST_BYTES = 1024 * 1024


def asset_path(value: str) -> bool:
    return bool(value) and all(
        part
        and not part.startswith(".")
        and "\\" not in part
        and not any(ord(char) < 32 or ord(char) == 127 for char in part)
        for part in value.split("/")
    )


def private_reference(descriptor: LessonModuleDescriptor) -> tuple[str, str] | None:
    parsed = urlsplit(descriptor.entry_url)
    if not parsed.path.startswith(REFERENCE_PREFIX):
        return None
    artifact, separator, encoded = parsed.path.removeprefix(REFERENCE_PREFIX).partition("/")
    entry = unquote(encoded, errors="strict")
    public = urlsplit(settings.public_base_url)
    if (
        not separator
        or not HASH.fullmatch(artifact)
        or not asset_path(entry)
        or encoded != quote(entry, safe="/-._~!'()*")
        or (parsed.scheme, parsed.netloc) != (public.scheme, public.netloc)
    ):
        raise ValueError("Private module references must use this Skills origin and a canonical package path")
    return artifact, entry


@contextmanager
def package_file(root: Path, artifact: str, name: str) -> Iterator[BinaryIO]:
    """Open every component relative to a directory FD; never follow a symlink."""
    if not root.is_absolute() or not HASH.fullmatch(artifact) or not asset_path(name):
        raise ValueError("Invalid private package path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", directory_flags)
    try:
        for part in (*root.parts[1:], artifact, *name.split("/")[:-1]):
            if part in ("", ".", ".."):
                raise ValueError("Invalid private package directory")
            child = os.open(part, directory_flags, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(name.split("/")[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Invalid private package file")
            yield stream
    finally:
        os.close(fd)


def read_file(root: Path, artifact: str, name: str, *, limit: int) -> bytes:
    with package_file(root, artifact, name) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise ValueError("Private package metadata is too large")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Private package metadata is too large")
        return data


@dataclass(frozen=True)
class Asset:
    path: str
    sha256: str
    size: int


def package_assets(descriptor: LessonModuleDescriptor) -> tuple[str, tuple[Asset, ...]]:
    reference = private_reference(descriptor)
    root = settings.private_lesson_modules_root
    if reference is None or root is None:
        raise ValueError("Private module storage is not configured")
    artifact, entry = reference
    manifest = json.loads(read_file(root, artifact, "manifest.json", limit=MAX_MANIFEST_BYTES))
    published = LessonModuleDescriptor.parse_raw(read_file(root, artifact, "module.json", limit=8192))
    definition = manifest["definition"]
    if (
        published != descriptor
        or manifest["artifact_sha256"] != artifact
        or definition["id"] != descriptor.id
        or definition["api_version"] != 1
        or definition["entry"] != entry
        or not isinstance(manifest["files"], list)
    ):
        raise ValueError("Private package does not match its registry reference")
    assets = []
    for item in manifest["files"]:
        name, digest, size = item["path"], item["sha256"], item["bytes"]
        if (
            not isinstance(name, str)
            or not asset_path(name)
            or name in ("module.json", "manifest.json")
            or not isinstance(digest, str)
            or not HASH.fullmatch(digest)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= 512 * 1024 * 1024
        ):
            raise ValueError("Invalid private asset inventory")
        assets.append(Asset(name, digest, size))
    names = [asset.path for asset in assets]
    if len(names) != len(set(names)) or entry not in names:
        raise ValueError("Invalid private asset inventory")
    return artifact, tuple(assets)


def checked_asset(descriptor: LessonModuleDescriptor, name: str) -> str:
    artifact, assets = package_assets(descriptor)
    asset = next((item for item in assets if item.path == name), None)
    root = settings.private_lesson_modules_root
    if root is None or asset is None:
        raise ValueError("Asset is not part of the reviewed package")
    # Nginx serves the bytes after this check. The operator-only publication root
    # must remain immutable; disable_symlinks also guards Nginx's separate open.
    digest = hashlib.sha256()
    with package_file(root, artifact, name) as stream:
        if os.fstat(stream.fileno()).st_size != asset.size:
            raise ValueError("Private asset differs from its reviewed inventory")
        total = 0
        while data := stream.read(256 * 1024):
            total += len(data)
            if total > asset.size:
                raise ValueError("Private asset differs from its reviewed inventory")
            digest.update(data)
    if total != asset.size or digest.hexdigest() != asset.sha256:
        raise ValueError("Private asset differs from its reviewed inventory")
    return ACCEL_PREFIX + artifact + "/" + quote(name, safe="/")


def grant_key(token: str) -> str:
    return "private_lesson_grant:" + hashlib.sha256(token.encode()).hexdigest()


async def issue_grant(
    descriptor: LessonModuleDescriptor, user: User | None, unit_id: str | None, course_id: str | None
) -> LessonModuleDescriptor:
    from api.services import rooms
    from api.services.courses import COURSES

    if user is None or (not user.email_verified and not user.admin):
        raise HTTPException(403, "Melde dich an, um diese Lektion zu öffnen.")
    content = rooms.catalogue()
    unit = next((item for item in content.units if item.id == unit_id and not item.retired), None)
    if unit is None or unit.module_id != descriptor.id:
        raise HTTPException(404, "Diese Lektion ist gerade nicht verfügbar.")
    candidates = [course for course in COURSES.values() if course.learning_path_id == unit.path_id]
    if course_id is not None:
        candidates = [course for course in candidates if course.id == course_id]
    admitted = None
    for course in candidates:
        try:
            await rooms.require_path_access(content, unit.path_id, user, course.id)
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
        else:
            admitted = course.id
            break
    if admitted is None:
        raise HTTPException(403, "Für diese Lektion brauchst du Zugang zum Kurs.")
    try:
        reference = private_reference(descriptor)
        assert reference is not None
        artifact, entry = reference
        await run_in_threadpool(checked_asset, descriptor, entry)
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "Diese Lektion kann gerade nicht geladen werden.") from None
    binding = {"user_id": user.id, "course_id": admitted, "unit_id": unit.id, "module": descriptor.dict()}
    serialized = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    # A module URL is also the player's component identity. Keep it stable even
    # when Redis expires/restarts; only a newly authorized API response creates
    # the short-lived access record. The derived value alone authorizes nothing.
    digest = hmac.new(
        settings.jwt_secret.encode(), b"private-lesson-module-assets-v1\0" + serialized.encode(), hashlib.sha256
    ).digest()
    token = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    await redis.setex(grant_key(token), settings.private_lesson_module_grant_ttl, serialized)
    return descriptor.copy(
        update={
            "entry_url": f"{settings.public_base_url.rstrip('/')}/lesson-assets/{token}/{artifact}/{quote(entry, safe='/')}"
        }
    )


async def asset_redirect(token: str, artifact: str, name: str) -> str:
    if not GRANT.fullmatch(token) or not HASH.fullmatch(artifact) or not asset_path(name):
        raise ValueError("Invalid asset request")
    raw = await redis.get(grant_key(token))
    if raw is None:
        raise ValueError("Missing or expired asset grant")
    binding: dict[str, Any] = json.loads(raw)
    descriptor = LessonModuleDescriptor.parse_obj(binding["module"])
    reference = private_reference(descriptor)
    if reference is None or reference[0] != artifact:
        raise ValueError("Asset grant does not cover this package")
    # Withdrawal/erasure invalidates access. Replacing a registry reference must
    # keep already-admitted, immutable old packages available until their TTL.
    current = await db.get(LessonModule, id=descriptor.id)
    guard = await db.get(PurchaseUser, user_id=binding["user_id"])
    if current is None or current.api_version != descriptor.api_version or (guard is not None and guard.deleted):
        raise ValueError("Asset grant is no longer available")
    return await run_in_threadpool(checked_asset, descriptor, name)
