"""
Shared base exception class for the application.
All feature specific exception should inherit from these base classes
"""

from ..utils.to_dict import build_error_response


class BaseAppException(Exception):
    """
    Base exception class for all application error.
    All custom exception should inherit from this class
    """

    def __init__(
        self, message: str, status_code: int = 500, details: dict | None = None
    ) -> None:
        """
        Initialize the base exception

        Args:
            message: Human-readable errors message
            status_code: HTTP status code (default = 500)
            details: Extra error details (default = None)
        """

        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict:
        """Convert exceptions to dictionary format"""
        return build_error_response(
            error_type=self.__class__.__name__,
            message=self.message,
            status_code=self.status_code,
            details=self.details,
        )


# Common HTTP exceptions used accross the application


class BadRequestError(BaseAppException):
    """Rated for invalid client request (400)"""

    def __init__(
        self, message: str, status_code: int, details: dict | None = None
    ) -> None:
        super().__init__(message=message, status_code=400, details=details)


class BadGatewayError(BaseAppException):
    """Raised when the server receives an invalid response from an upstream server (502)"""

    def __init__(self, message: str = "Bad Gateway", details: dict | None = None):
        super().__init__(message=message, status_code=502, details=details)


class UnauthorizedError(BaseAppException):
    """Raised when authentication fails (401)"""

    def __init__(self, message: str = "Unauthorized", details: dict | None = None):
        super().__init__(message=message, status_code=401, details=details)


class NotFoundError(BaseAppException):
    """Raised when resource is not found (404)"""

    def __init__(self, message: str = "Not Found", details: dict | None = None):
        super().__init__(message=message, status_code=404, details=details)


class ForbiddenError(BaseAppException):
    """Raised when user lacks permission (403)"""

    def __init__(self, message: str = "Forbidden", details: dict | None = None):
        super().__init__(message=message, status_code=403, details=details)


class ConflictError(BaseAppException):
    """Raised when there's a resource conflict (409)"""

    def __init__(self, message: str = "Conflict", details: dict | None = None):
        super().__init__(message=message, status_code=409, details=details)
