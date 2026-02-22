from sqlmodel import SQLModel, Field
from enum import Enum
from datetime import datetime

class User(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str
    email: str
    passwordhash: str

class Project(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    name: str
    user_id: int = Field(foreign_key="user.id")
    description: str
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_HUMAN = "waiting_for_human"

class Priority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"



class TaskBase(SQLModel):
    """Shared fields for Task (table) and API create schema. No table."""

    title: str
    user_id: int = Field(foreign_key="user.id")
    assignee_id: int | None = Field(default=None, foreign_key="user.id")
    project_id: int = Field(foreign_key="project.id")
    description: str
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    priority: Priority = Field(default=Priority.MEDIUM)
    pending_question: str | None = Field(default=None)


class Task(TaskBase, table=True):
    id: int = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class DashboardToken(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    user_id: int = Field(foreign_key="user.id")
    chat_id: int  # Telegram chat_id for reference
    created_at: datetime = Field(default_factory=datetime.now)
