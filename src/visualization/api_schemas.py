"""Pydantic schemas for the Tasks REST API (request/response only)."""

from models import Priority, Task, TaskBase, TaskStatus
from sqlmodel import SQLModel


class TaskRead(Task, table=False):
    """Task response schema: same shape as Task, no table."""

    model_config = {"from_attributes": True}


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
