import pytest
from sqlalchemy import create_engine
from sqlmodel import SQLModel, Session

import db
from models import User, Project, Task  # noqa: F401 — register tables


@pytest.fixture
def in_memory_engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(db, "init_db", lambda: None)
    return engine


@pytest.fixture
def db_session(in_memory_engine):
    with Session(in_memory_engine) as session:
        yield session


@pytest.fixture
def seed_user_project(db_session):
    """Insert one User and one Project; return (user_id, project_id) for task_service tests."""
    user = User(name="Test User", email="test@example.com", passwordhash="hash")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    project = Project(name="Test Project", user_id=user.id, description="Test")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return user.id, project.id
