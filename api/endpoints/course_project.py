"""Private course-wide project state that every lesson of a course can read and extend."""

from fastapi import APIRouter, Depends

from api.auth import require_verified_email, user_auth
from api.endpoints.rooms import enabled
from api.schemas.course_project import ProjectEnvelope, SaveProject
from api.schemas.user import User
from api.services import course_project

router = APIRouter(prefix="/courses/{course_id}/project", dependencies=[Depends(enabled), require_verified_email])


@router.get("", response_model=ProjectEnvelope)
async def get_project(course_id: str, user: User = user_auth) -> ProjectEnvelope:
    """The learner's own project state; revision 0 and an empty object before the first save."""
    return await course_project.get_project(course_id, user)


@router.put("", response_model=ProjectEnvelope)
async def save_project(course_id: str, data: SaveProject, user: User = user_auth) -> ProjectEnvelope:
    """Replace the state at `expected_revision`; an exact retry of `request_id` returns the saved result."""
    return await course_project.save_project(course_id, user, data)
