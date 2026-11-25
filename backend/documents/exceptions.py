from rest_framework import status
from rest_framework.exceptions import APIException


class Conflict(APIException):
    """The request is valid but conflicts with the document's current state."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = "The document is not in a state that allows this action."
    default_code = "conflict"


class ServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Processing could not be started. Please try again shortly."
    default_code = "service_unavailable"
