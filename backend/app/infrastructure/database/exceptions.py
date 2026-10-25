"""
Database-specific exception classes.
All database-related errors should inherit from DatabaseError
"""

from app.shared.errors.exceptions import BaseAppException


# Added 2026-09-07 11:19 PST — put infrastructure errors under BaseAppException.
# This hierarchy derived straight from Exception, and main.py only registers a
# handler for BaseAppException, so nothing caught it: every raise site in
# base_repository (7 of them) and the SQLAlchemyError conversion in
# sessions.py:46 escaped as an unhandled 500 with FastAPI's default body
# instead of the app's error shape. `status_code` was dead — set on every
# instance, read by nobody. `details` was typed `str | None` here and
# `dict | None` on BaseAppException; the base type wins.
class InfrastructureError(BaseAppException):
    """
        Exception class for all database-related errors.
        All database-specific exception should inherit from this class.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: dict | None = None,
        original_error: Exception | None = None
    ):
        """
            Initialize the database error

            Args:
                message: Human-readable erroe messages
                status_code: HTTP status code (default: 500)
                details: Extra error details (default: None)
                original_error: The original SQLALchemy or database error (default: None)
        """

        # Kept off to_dict() on purpose — the driver error can carry SQL
        # fragments and connection strings, so it stays in the logs.
        self.original_error = original_error
        super().__init__(message=message, status_code=status_code, details=details)


class DatabaseError(InfrastructureError):
    """Database Operation Failed"""
    ...
    
class DatabaseConnectionError(DatabaseError):
    """Cannot connect to database"""
    ...

class IntegrityConstraintError(DatabaseError):
    """Unique constraint, foreign key, check constraint violated."""
    ...
class SchemaOutOfDateError(DatabaseError):
    """The database is behind the migrations the code was written against."""
    ...
