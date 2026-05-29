from typing import AsyncGenerator

from app.core import settings
from app.infrastructure.observability.logging_setup import log
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .exceptions import DatabaseConnectionError

async_engine: AsyncEngine = create_async_engine(
    settings.DB_URL,
    echo=settings.DB_ECHO,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
)

AsyncSessionLocal = async_sessionmaker(
    async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides a database session.
    Automatically handles commitr/rollback and cleanup
    """

    async with AsyncSessionLocal() as session:
        try:
            log.debug("Database session created")
            yield session
            await session.commit()
        except SQLAlchemyError as e:
            await session.rollback()
            log.error("Database error: ", exc_info=True)
            raise DatabaseConnectionError(
                f"Database operation failed: {str(e)}", original_error=e
            )
        except Exception as e:
            await session.rollback()
            log.error(f"Unexpected error: {str(e)}", exc_info=True)
            raise
        finally:
            await session.close()


async def close_db():
    """
    Close database connections.
    Call this on application shutdown.
    """
    await async_engine.dispose()
    log.info("Database connection closed")
