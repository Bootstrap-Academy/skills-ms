from starlette import status

from api.exceptions.api_exception import APIException


class SkillNotCompletedError(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    detail = "Skill not completed"
    description = "The user has not completed the skill."


class CertificateNotFoundError(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Certificate not found"
    description = "The certificate does not exist."
