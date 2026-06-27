import structlog
from app.core import settings

def configure_logger():
    """
    Configure logging based on environment

    Development: Pretty console output with colors 
    Prodcution: JSON logs for parsing/aggregation
    """
    if settings.ENVIRONMENT == "production":
        structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory()
        )
    else:
        structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            structlog.dev.ConsoleRenderer(exception_formatter=structlog.dev.plain_traceback),
        ],
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        )

configure_logger()
log = structlog.get_logger()

