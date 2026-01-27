# inventory_hub/database.py
"""
PostgreSQL database connection for Inventory Hub v12 FINAL.

Uses SQLAlchemy 2.0 async with asyncpg driver.
"""
from __future__ import annotations
from typing import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event, text

from inventory_hub.settings import settings

# ============================================================================
# Base class for all ORM models
# ============================================================================

class Base(DeclarativeBase):
    """Base class for all database models."""
    pass


# ============================================================================
# Engine and Session Factory
# ============================================================================

_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_database_url() -> str:
    """Build async database URL from settings."""
    # Expected format: postgresql+asyncpg://user:pass@host:port/dbname
    return (
        f"postgresql+asyncpg://"
        f"{settings.DB_USER}:{settings.DB_PASSWORD}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )


def get_sync_database_url() -> str:
    """Build sync database URL (for migrations, etc.)."""
    return (
        f"postgresql://"
        f"{settings.DB_USER}:{settings.DB_PASSWORD}@"
        f"{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
    )


async def init_db() -> None:
    """Initialize database engine and session factory."""
    global _engine, _async_session_factory
    
    if _engine is not None:
        return  # Already initialized
    
    _engine = create_async_engine(
        get_database_url(),
        echo=settings.DB_ECHO,  # Log SQL queries if DEBUG
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,  # Verify connections before use
    )
    
    _async_session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def close_db() -> None:
    """Close database connections."""
    global _engine, _async_session_factory
    
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency for FastAPI - provides database session.
    
    Usage:
        @router.get("/items")
        async def get_items(db: AsyncSession = Depends(get_session)):
            ...
    """
    if _async_session_factory is None:
        await init_db()
    
    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """
    Context manager for database session (for use outside FastAPI).
    
    Usage:
        async with get_session_context() as db:
            result = await db.execute(...)
    """
    if _async_session_factory is None:
        await init_db()
    
    async with _async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ============================================================================
# Health Check
# ============================================================================

async def check_db_health() -> dict:
    """Check database connectivity and return status."""
    try:
        async with get_session_context() as db:
            result = await db.execute(text("SELECT 1"))
            result.scalar()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}


# ============================================================================
# Transaction Helpers
# ============================================================================

@asynccontextmanager
async def transaction(session: AsyncSession):
    """
    Explicit transaction context for critical operations.
    
    Usage:
        async with transaction(db):
            await db.execute(...)
            await db.execute(...)
        # Commits on success, rolls back on exception
    """
    try:
        yield
        await session.commit()
    except Exception:
        await session.rollback()
        raise
