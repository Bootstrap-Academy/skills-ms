from starlette import status

from api.exceptions.api_exception import APIException


class CourseNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Course not found"
    description = "The requested course does not exist."


class LectureNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Lecture not found"
    description = "The requested lecture does not exist."


class NextLectureNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "No lecture suggestion available"
    description = "The user does not currently have a lecture recommendation."


class NextTaskNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "No task suggestion available"
    description = "The user does not currently have a task recommendation."


class NextLabNotFoundException(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "No lab suggestion available"
    description = "The user does not currently have a lab recommendation."


class NoCourseAccessException(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "No course access"
    description = "You do not have access to this course."


class CourseIsFreeException(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "Course is free"
    description = "This course is free and does not require payment."


class AlreadyPurchasedCourseException(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "Already purchased course"
    description = "You have already purchased this course."


class NotEnoughCoinsError(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "Not enough coins"
    description = "The user does not have enough coins to perform this action."


class AlreadyCompletedLectureException(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    detail = "Already completed lecture"
    description = "You have already completed this lecture."
