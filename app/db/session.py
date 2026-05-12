"""Database connection and session management.

Supabase notes:
- Use the Transaction pooler URL (port 6543), not the direct connection (5432).
  The pooler is PgBouncer in transaction mode — compatible with asyncpg.
- Append ?ssl=require to DATABASE_URL. asyncpg reads this and enables TLS.
- statement_cache_size=0 and prepared_statement_cache_size=0 in connect_args
  disable asyncpg's prepared statement cache, which pgbouncer does not support.
- pool_size / max_overflow are ignored by PgBouncer (it manages its own pool),
  but we keep them small to avoid exhausting the 15-connection Supabase free tier.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    # Supabase Transaction pooler (pgbouncer) does not support prepared statements.
    # statement_cache_size=0 disables them in asyncpg.
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    },
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

Base = declarative_base()


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a DB session and closes it on exit."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create all tables (used in development / first-run)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    """Dispose the connection pool on shutdown."""
    await engine.dispose()
