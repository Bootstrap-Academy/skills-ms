from typing import Any
from unittest.mock import AsyncMock

from pytest_mock import MockerFixture

from api import models
from api.database import db, db_context
from api.endpoints.course import get_accessible_courses, get_unlocked_courses, list_courses
from api.schemas.course import Course
from api.schemas.user import User
from api.utils.utc import utcnow


def _course(course_id: str, price: int) -> Course:
    return Course(
        id=course_id,
        title=course_id,
        description=None,
        category=None,
        language=None,
        image=None,
        authors=[],
        price=price,
        learning_goals=[],
        requirements=[],
        last_update=0,
        sections=[],
    )


COURSES = {"free": _course("free", 0), "paid": _course("paid", 100), "other": _course("other", 200)}


def _user(admin: bool = False) -> User:
    return User(id="user", email_verified=True, admin=admin)


def _setup(mocker: MockerFixture, *, premium: bool, owned: set[str] | None = None, admin: bool = False) -> User:
    mocker.patch("api.endpoints.course.COURSES", COURSES)
    mocker.patch("api.endpoints.course.has_premium", AsyncMock(return_value=premium))
    mocker.patch("api.endpoints.course.get_owned_courses", AsyncMock(return_value=owned or set()))
    return _user(admin=admin)


def _ids(courses: Any) -> set[str]:
    return {course.id for course in courses}


async def test__get_unlocked_courses__owned_only(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=False, owned={"paid", "deleted_course"})

    assert await get_unlocked_courses(user) == {"paid"}


async def test__get_unlocked_courses__premium_unlocks_everything(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=True)

    assert await get_unlocked_courses(user) == set(COURSES)


async def test__get_unlocked_courses__admin_unlocks_everything(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=False, admin=True)

    assert await get_unlocked_courses(user) == set(COURSES)


async def test__get_accessible_courses__free_and_owned(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=False, owned={"paid"})

    async with db_context():
        assert _ids(await get_accessible_courses(user)) == {"free", "paid"}


async def test__get_accessible_courses__premium_includes_paid_courses(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=True)

    async with db_context():
        assert _ids(await get_accessible_courses(user)) == set(COURSES)


async def test__get_accessible_courses__reports_the_progress(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=True)

    async with db_context():
        await db.add(models.LectureProgress(user_id="user", course_id="paid", lecture_id="lecture", completed=utcnow()))

        courses = {course.id: course for course in await get_accessible_courses(user)}

    assert courses["paid"].completed is True


async def _list(user: User | None, owned: bool | None) -> Any:
    return await list_courses(
        search_term=None, language=None, author=None, free=None, owned=owned, recent_first=False, user=user
    )


async def test__list_courses__owned_without_premium(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=False, owned={"paid"})

    async with db_context():
        assert _ids(await _list(user, True)) == {"paid"}
        assert _ids(await _list(user, False)) == {"free", "other"}


async def test__list_courses__owned_with_premium(mocker: MockerFixture) -> None:
    user = _setup(mocker, premium=True)

    async with db_context():
        assert _ids(await _list(user, True)) == set(COURSES)
        assert _ids(await _list(user, False)) == set()


async def test__list_courses__owned_without_a_user(mocker: MockerFixture) -> None:
    _setup(mocker, premium=True)

    async with db_context():
        assert _ids(await _list(None, True)) == set()
        assert _ids(await _list(None, False)) == set(COURSES)
