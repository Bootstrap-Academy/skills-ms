"""The course catalogue of the repository (`config/courses`)."""

from api.services.courses import COURSES


def test_every_course_of_the_catalogue_is_free() -> None:
    # Courses are no longer sold one by one; every learner may open every course.
    # A price above 0 would make that course purchasable again (`purchases.offer`).
    assert COURSES
    assert sorted(course.id for course in COURSES.values() if not course.free) == []
