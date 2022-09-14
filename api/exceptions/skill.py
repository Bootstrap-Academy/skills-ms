from starlette import status

from api.exceptions.api_exception import APIException


class SkillNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Skill not found"
    description = "The requested skill does not exist."


class SkillAlreadyExistsException(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Skill already exists"
    description = "The requested skill already exists."


class CycleInSkillTreeException(APIException):
    status_code = status.HTTP_409_CONFLICT
    detail = "Cycle in skill tree"
    description = "The requested skill tree contains a cycle."
