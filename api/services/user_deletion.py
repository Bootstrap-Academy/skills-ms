from typing import Any

from api import models
from api.database import db, delete
from api.utils.cache import clear_cache


# Live service data removed on account erasure. Purchase evidence is retained separately.
USER_MODELS: list[Any] = [
    models.RoomRequest,
    models.RoomState,
    models.CourseAccess,
    models.LastWatch,
    models.LectureProgress,
    models.SubSkillBookmark,
    models.XP,
]

# Durable claims and deletion tombstones must not recreate service access.
RETAINED_USER_MODELS: list[Any] = [
    models.CoursePurchase,
    models.PurchaseUser,
    models.RetainedCourseRight,
    models.CourseRightGrant,
    models.XPOperation,
]

# all cache prefixes that contain data which belongs to a specific user
USER_CACHE_PREFIXES = ["course_access", "lecture_progress", "skills", "user", "xp"]


async def delete_user_data(user_id: str) -> None:
    """Remove live service data and retain purchase obligations with a deletion tombstone."""

    from api.database import filter_by
    from api.models.purchase import CoursePurchase
    from api.services.purchases import lock_user

    guard = await lock_user(user_id)
    from api.services.retained_rights import preserve_before_erasure

    await preserve_before_erasure(user_id)
    guard.deleted = True
    for purchase in await db.all(
        filter_by(CoursePurchase, user_id=user_id)
        .where(CoursePurchase.state.in_(["prepared", "paid"]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        purchase.state = "review"

    for model in USER_MODELS:
        await db.exec(delete(model).where(model.user_id == user_id))

    for prefix in USER_CACHE_PREFIXES:
        await clear_cache(prefix)
