from typing import Any

from api import models
from api.database import db, delete
from api.utils.cache import clear_cache


# all models that contain data which belongs to a specific user
USER_MODELS: list[Any] = [
    models.CourseAccess,
    models.LastWatch,
    models.LectureProgress,
    models.SubSkillBookmark,
    models.XP,
]

# all cache prefixes that contain data which belongs to a specific user
USER_CACHE_PREFIXES = ["course_access", "lecture_progress", "skills", "user", "xp"]


async def delete_user_data(user_id: str) -> None:
    """Delete all data of a user."""

    for model in USER_MODELS:
        await db.exec(delete(model).where(model.user_id == user_id))

    for prefix in USER_CACHE_PREFIXES:
        await clear_cache(prefix)
