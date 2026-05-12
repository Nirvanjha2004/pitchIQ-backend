"""Database connection and session management.

Supabase notes:
- Use the Transaction pooler URL (port 6543), not the direct connection (5432).
  The pooler is PgBouncer in transaction mode — compatible with asyncpg.
- Append ?ssl=require to DATABASE_URL. asyncpg reads this and enables TLS.
- prepared_statement_cache_size=0 is appended to the URL at runtime to disable
  asyncpg's prepared statement cache, which pgbouncer does not support.
- pool_size / max_overflow are ignored by PgBouncer (it manages its own pool),
  but we keep them small to avoid exhausting the 15-connection Supabase free tier.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

# Build the URL with statement_cache_size=0 appended.
# This is the most reliable way to disable prepared statements for asyncpg
# when using Supabase's Transaction pooler (pgbouncer in transaction mode).
_db_url = settings.DATABASE_URL
if "statement_cache_size" not in _db_url:
    _separator = "&" if "?" in _db_url else "?"
    _db_url = f"{_db_url}{_separator}prepared_statement_cache_size=0"

engine = create_async_engine(
    _db_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args={"statement_cache_size": 0},
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
