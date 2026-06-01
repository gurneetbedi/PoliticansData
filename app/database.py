"""Database setup — SQLite for development, swap DATABASE_URL for Postgres in production."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./politrack.db")

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
