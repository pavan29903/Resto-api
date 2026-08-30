"""Async database engine and session.

Points at Supabase Postgres in every environment; there is no separate local
database, which keeps dev and production on identical schema behaviour.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is not set. Copy it from your Supabase project: "
                "Settings -> Database -> Connection string -> URI, and set it in .env"
            )
        _engine = create_async_engine(
            settings.async_database_url,
            echo=False,
            pool_pre_ping=True,
            # Supabase's pooler already pools; keep our own pool small so a
            # free-tier connection cap isn't exhausted by one backend instance.
            pool_size=5,
            max_overflow=5,
            # Supabase requires TLS. Note we connect through the *session*
            # pooler (port 5432) rather than the direct host, which is
            # IPv6-only and unreachable from IPv4-only networks.
            connect_args={"ssl": "require"},
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — one session per request, rolled back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
