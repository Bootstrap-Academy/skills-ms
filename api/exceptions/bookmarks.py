from starlette import status

from api.exceptions.api_exception import APIException


class AlreadyBookmarkedException(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Skill already bookmarked"
    description = "The requested skill is already bookmarked."


class BookmarkNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Bookmark not found"
    description = "The requested bookmark was not found."
