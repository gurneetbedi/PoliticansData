"""Database setup — SQLite for local development, Neon Postgres in production.

Environment-based selection:
    - On Render (RENDER=true env var, set automatically by Render) → use DATABASE_URL (Neon).
    - Local dev → use SQLite (lokvani.db) regardless of a stray DATABASE_URL in the shell.
    - Escape hatch: set USE_NEON=1 locally to force Neon (for testing against prod DB).

This prevents the common trap where a `secrets/.env` sourced into the local shell
silently redirects local uvicorn to Neon.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

_LOCAL_SQLITE = "sqlite:///./lokvani.db"
_IS_PROD      = os.getenv("RENDER", "").lower() in ("true", "1", "yes")
_FORCE_NEON   = os.getenv("USE_NEON", "").lower() in ("true", "1", "yes")

if _IS_PROD or _FORCE_NEON:
    DATABASE_URL = os.getenv("DATABASE_URL", _LOCAL_SQLITE)
else:
    # Local dev: always SQLite, ignoring any stray DATABASE_URL.
    DATABASE_URL = _LOCAL_SQLITE

# Neon / Heroku / many managed-Postgres hosts emit URLs starting with `postgres://`,
# but SQLAlchemy 2.x rejects that scheme and requires `postgresql://`. Normalize so
# the same code path works locally (SQLite) and in production (Postgres).
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# SQLite needs a special flag for FastAPI's threading model
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

# pool_pre_ping survives Neon's idle-connection drops (it pings before each checkout
# and silently reconnects if the connection went stale).
engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=not DATABASE_URL.startswith("sqlite"),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency for request-scoped DB sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
