from starlette import status

from api.exceptions.api_exception import APIException


class DataFetchError(APIException):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    detail = "Failed to fetch task data"
    description = "Failed to fetch task data from the database"
