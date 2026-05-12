"""Database connection and session management.

Supabase notes:
- Use the Transaction pooler URL (port 6543), not the direct connection (5432).
  The pooler is PgBouncer in transaction mode — compatible with asyncpg.
- Append ?ssl=require to DATABASE_URL. asyncpg reads this and enables TLS.
- pool_size / max_overflow are ignored by PgBouncer (it manages its own pool),
  but we keep them small to avoid exhausting the 15-connection Supabase free tier.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

# ssl=require is carried in the URL query string (?ssl=require).
# asyncpg parses it natively — no extra connect_args needed.
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
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
