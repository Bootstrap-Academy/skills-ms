from starlette import status

from api.exceptions.api_exception import APIException


class CourseNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Course not found"
    description = "The requested course does not exist."
