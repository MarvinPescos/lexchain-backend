"""
Database-specific exception classes.
All database-related errors should inherit from DatabaseError
"""

class InfrastructureError(Exception):
    """
        Exception class for all database-related errors.
        All database-specific exception should inherit from this class.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        details: str | None = None,
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

        self.message = message
        self.status_code = status_code
        self.details = details
        self.original_error = original_error
        super().__init__(self.message)
       
class DatabaseError(InfrastructureError):
    """Database Operation Failed"""
    ...
    
class DatabaseConnectionError(DatabaseError):
    """Cannot connect to database"""
    ...

class IntegrityConstraintError(DatabaseError):
    """Unique constraint, foreign key, check constraint violated."""
    ...