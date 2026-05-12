"""FastAPI app entry point for PitchIQ"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import auth
from app.api import gateway as gateway_api
from app.api import tasks as tasks_api
from app.config import settings
from app.db.session import close_db
from app.utils.logger import get_logger

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic"""
    logger.info("Starting PitchIQ backend...")
    yield
    logger.info("Shutting down PitchIQ backend...")
    await close_db()


app = FastAPI(
    title="PitchIQ",
    description="AI-powered cold outreach — LLM Inference Gateway + Agent Orchestration",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])

# LLM Inference Gateway
app.include_router(gateway_api.router, prefix="/api/v1", tags=["gateway"])

# Agent Orchestration
app.include_router(tasks_api.router, prefix="/api/v1", tags=["tasks"])


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "healthy", "service": "pitchiq"}
