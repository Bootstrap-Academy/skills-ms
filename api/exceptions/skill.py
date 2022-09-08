from starlette import status

from api.exceptions.api_exception import APIException


class SkillNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Skill not found"
    description = "The requested skill does not exist."
