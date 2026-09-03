import asyncio

from httpx import HTTPError
from sqlalchemy import union
from sqlalchemy.future import select as sa_select
from sqlalchemy.sql.selectable import Select

from .database import db, db_context
from .logger import get_logger
from .services.auth import get_user_status
from .services.internal import InternalServiceError
from .services.user_deletion import USER_MODELS, delete_user_data
from .settings import settings


logger = get_logger(__name__)


def _user_ids_query(after: str | None, limit: int) -> Select:
    """Return a query for the next batch of distinct user ids in the database."""

    user_ids = union(*[sa_select(model.user_id) for model in USER_MODELS]).subquery()

    query = sa_select(user_ids.c.user_id)
    if after is not None:
        query = query.where(user_ids.c.user_id > after)

    return query.order_by(user_ids.c.user_id).limit(limit)


async def sweep_deleted_users() -> None:
    """Delete all data of users whose accounts no longer exist."""

    delay = 1 / settings.deleted_user_sweep_rate_limit if settings.deleted_user_sweep_rate_limit > 0 else 0
    checked = missing = deleted = errors = 0
    after: str | None = None

    while True:
        async with db_context():
            user_ids: list[str] = await db.all(_user_ids_query(after, settings.deleted_user_sweep_batch_size))

        if not user_ids:
            break

        after = user_ids[-1]

        for user_id in user_ids:
            await asyncio.sleep(delay)
            checked += 1

            try:
                status_code = await get_user_status(user_id)
            except (InternalServiceError, HTTPError):
                errors += 1
                continue

            if status_code == 404:
                missing += 1
                async with db_context():
                    await delete_user_data(user_id)
                deleted += 1
            elif status_code != 200:
                errors += 1

    logger.info(f"sweep finished: checked={checked} missing={missing} deleted={deleted} errors={errors}")


def main() -> None:
    asyncio.run(sweep_deleted_users())
