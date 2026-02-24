"""PostgreSQL connection and schema init for openshrimp.

Uses psycopg2 and SQLModel; connection params from env (POSTGRES_*).
"""

import os
from contextlib import contextmanager

import psycopg2
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from models import Task, Project, User, DashboardToken  # noqa: F401 — ensure all tables registered


def _connection_params():
    return {
        "host": os.environ.get("POSTGRES_HOST", "localhost"),
        "port": int(os.environ.get("POSTGRES_PORT", "5432")),
        "user": os.environ.get("POSTGRES_USER", "postgres"),
        "password": os.environ.get("POSTGRES_PASSWORD", ""),
        "dbname": os.environ.get("POSTGRES_DB", "postgres"),
    }


def _database_url() -> str:
    p = _connection_params()
    return (
        f"postgresql+psycopg2://{p['user']}:{p['password']}"
        f"@{p['host']}:{p['port']}/{p['dbname']}"
    )


def get_connection():
    """Return a new psycopg2 connection. Caller must close it."""
    return psycopg2.connect(**_connection_params())


@contextmanager
def connection():
    """Context manager that yields a psycopg2 connection and closes it on exit."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


_engine = None


def get_engine():
    """Return a shared SQLAlchemy engine (postgresql+psycopg2) for SQLModel sessions."""
    global _engine
    if _engine is None:
        _engine = create_engine(_database_url(), echo=False)
    return _engine


def _migrate_add_columns(engine) -> None:
    """Idempotently add new columns to existing tables (ALTER TABLE IF NOT EXISTS)."""
    sqls = [
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS effort VARCHAR DEFAULT 'normal'",
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS worker_id VARCHAR",
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP",
        "ALTER TABLE task ADD COLUMN IF NOT EXISTS chat_id BIGINT",
    ]
    import sqlalchemy
    with engine.connect() as conn:
        for sql in sqls:
            try:
                conn.execute(sqlalchemy.text(sql))
                conn.commit()
            except Exception as exc:
                conn.rollback()
                # Column may already exist with a different error path — log and continue
                import logging
                logging.getLogger("db").debug("Migration skipped (%s): %s", sql, exc)


def init_db() -> None:
    """Create all tables from SQLModel metadata if they do not exist."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _migrate_add_columns(engine)
