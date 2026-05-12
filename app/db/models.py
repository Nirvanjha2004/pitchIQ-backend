"""SQLAlchemy models for PitchIQ"""

from sqlalchemy import Column, String, Integer, Float, DateTime, JSON, ForeignKey, Text, func
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.session import Base

try:
    from pgvector.sqlalchemy import Vector
    PGVECTOR_AVAILABLE = True
except ImportError:
    PGVECTOR_AVAILABLE = False


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, unique=True, index=True)
    password_hash = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("Task", back_populates="user")
    usage = relationship("TokenUsage", back_populates="user")


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"))
    status = Column(String, default="pending")
    input_data = Column(JSON)
    output_data = Column(JSON)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    user = relationship("User", back_populates="tasks")
    usage = relationship("TokenUsage", back_populates="task")


class TokenUsage(Base):
    """Tracks every LLM call including critic calls."""
    __tablename__ = "token_usage"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    model_used = Column(String, nullable=False)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="usage")
    task = relationship("Task", back_populates="usage")


class PromptCache(Base):
    """Semantic cache for LLM responses using pgvector."""
    __tablename__ = "prompt_cache"

    id = Column(String, primary_key=True)
    prompt_text = Column(Text, nullable=False)
    # embedding stored as JSON array when pgvector not available at model-definition time
    # The actual vector column is created via raw SQL migration
    response_text = Column(Text, nullable=False)
    model_used = Column(String, nullable=False)
    quality_score = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())
