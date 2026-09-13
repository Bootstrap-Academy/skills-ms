"""Internal CLI: register a reviewed module descriptor, without starting the API."""

import argparse
import asyncio
from pathlib import Path

from api.database import db, db_context
from api.schemas.lesson_module import LessonModuleDescriptor
from api.services.lesson_modules import checked_descriptor, register_module


async def register(descriptor: LessonModuleDescriptor, replace: bool) -> None:
    try:
        async with db_context():
            await register_module(descriptor, replace=replace)
    finally:
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Reviewed JSON descriptor: id, api_version, entry_url")
    parser.add_argument(
        "--replace", action="store_true", help="Replace this ID's previously reviewed artifact reference"
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate schema and configured origin without database access"
    )
    args = parser.parse_args()
    try:
        descriptor = checked_descriptor(LessonModuleDescriptor.parse_raw(args.manifest.read_text()))
        if not args.check:
            asyncio.run(register(descriptor, args.replace))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Module registration rejected: {exc}\n")
    print(f"{descriptor.id}: {'checked' if args.check else 'registered'} (API {descriptor.api_version})")


if __name__ == "__main__":
    main()
