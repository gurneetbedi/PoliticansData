"""Database setup — SQLite for development, swap DATABASE_URL for Postgres in production."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./politrack.db")

# SQLite needs a special flag for FastAPI's threading model
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency for request-scoped DB sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
