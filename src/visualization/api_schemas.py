"""Pydantic schemas for the Tasks REST API (request/response only)."""

from datetime import datetime

from models import Effort, Priority, Task, TaskBase, TaskStatus
from sqlmodel import SQLModel


class TaskRead(SQLModel):
    """Task response schema: explicit fields only (no ORM inheritance) so Pydantic serialization never sees InstrumentedAttribute."""

    id: int
    title: str
    user_id: int
    project_id: int
    description: str
    status: TaskStatus
    priority: Priority
    assignee_id: int | None = None
    pending_question: str | None = None
    effort: Effort = Effort.NORMAL
    created_at: datetime
    updated_at: datetime


class TaskCreate(TaskBase):
    """Request body for creating a task. Reuses TaskBase fields."""


class TaskUpdate(SQLModel):
    """Request body for partial task update."""

    title: str | None = None
    description: str | None = None
    project_id: int | None = None
    status: TaskStatus | None = None
    priority: Priority | None = None
    assignee_id: int | None = None
