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
        self,
        message: str,
        status_code: int = 500,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """
        Initialize the base exception

        Args:
            message: Human-readable errors message
            status_code: HTTP status code (default = 500)
            details: Extra error details (default = None)
            headers: Response headers this error must carry (default = None)
        """

        self.message = message
        self.status_code = status_code
        self.details = details
        # Added 2026-09-07 13:46 PST — carry response headers.
        # The auth guards had to raise HTTPException purely because a 401 owes
        # the client a WWW-Authenticate header and there was nowhere to put one,
        # which meant every auth failure answered in FastAPI's {"detail": ...}
        # shape instead of this one. Handled in main.app_exception_handler.
        self.headers = headers
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
        self, message: str = "Bad Request", details: dict | None = None
    ) -> None:
        super().__init__(message=message, status_code=400, details=details)


class BadGatewayError(BaseAppException):
    """Raised when the server receives an invalid response from an upstream server (502)"""

    def __init__(self, message: str = "Bad Gateway", details: dict | None = None):
        super().__init__(message=message, status_code=502, details=details)


class UnauthorizedError(BaseAppException):
    """Raised when authentication fails (401)"""

    def __init__(
        self,
        message: str = "Unauthorized",
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ):
        # A 401 has to name the scheme it wants (RFC 7235 §3.1), and this API
        # only accepts bearer tokens — so it is the default rather than
        # something each raise site has to remember.
        super().__init__(
            message=message,
            status_code=401,
            details=details,
            headers=headers or {"WWW-Authenticate": "Bearer"},
        )


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


# Added 2026-09-12 16:53 PST — a dependency being unreachable is not the
# caller's fault. Without this the only honest options were 401 (which tells a
# client its credentials are bad, so the frontend clears the session) or 500
# (which reads as "this app is broken"). 503 says "try again", which is what a
# client should actually do when Supabase, MinerU or OpenRouter is down.
# BadGatewayError stays for the other case: the dependency answered, but the
# answer was unusable.
class ServiceUnavailableError(BaseAppException):
    """Raised when a dependency is unreachable (503)"""

    def __init__(
        self,
        message: str = "Service Unavailable",
        details: dict | None = None,
        retry_after: int | None = None,
    ):
        # Retry-After rides the headers slot on BaseAppException so the client
        # can back off for a known interval instead of guessing.
        super().__init__(
            message=message,
            status_code=503,
            details=details,
            headers={"Retry-After": str(retry_after)} if retry_after else None,
        )


# Added 2026-09-07 23:06 PST — map framework status codes back to these names.
# main.py now renders Starlette's own HTTPException (404 from the router, 405
# from a wrong method, 401 from HTTPBearer when the Authorization header is
# missing) into the same body as these classes. Those exceptions carry no
# error_type, so without this map the client would get "NotFoundError" for a
# 404 the service raised and a bare "HTTPError" for a 404 the router raised —
# the same status reported under two different names. Keyed off __name__ so a
# class rename cannot leave the map behind.
HTTP_ERROR_TYPES: dict[int, str] = {
    400: BadRequestError.__name__,
    401: UnauthorizedError.__name__,
    403: ForbiddenError.__name__,
    404: NotFoundError.__name__,
    409: ConflictError.__name__,
    502: BadGatewayError.__name__,
    503: ServiceUnavailableError.__name__,
}
