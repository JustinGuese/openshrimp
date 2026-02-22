"""Task tracking plugin for openshrimp.

Thin @tool wrappers over task_service. No DB logic lives here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from langchain_core.tools import tool

# Ensure src/ is on sys.path when loaded via plugin loader
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import task_service
from schemas import ToolResult

PLUGIN_NAME = "task_tracking"


def _default_user_id() -> int | None:
    raw = os.environ.get("DEFAULT_USER_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _ok(data: str, metadata: dict | None = None) -> str:
    return ToolResult(status="ok", data=data, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


def _err(msg: str, metadata: dict | None = None) -> str:
    return ToolResult(status="error", data=msg, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


@tool
def create_task(
    title: str,
    description: str,
    project_id: int,
    user_id: int | None = None,
    priority: str = "medium",
    status: str = "pending",
) -> str:
    """Create a new task.

    Use this when the user wants to record a new task. Requires title, description, and project_id.
    user_id is optional if DEFAULT_USER_ID is set in the environment.

    Args:
        title: Short title for the task.
        description: Full description of the task.
        project_id: ID of the project this task belongs to.
        user_id: Owner user ID (optional if DEFAULT_USER_ID is set).
        priority: One of low, medium, high. Default medium.
        status: One of pending, in_progress, completed, failed. Default pending.
    """
    uid = user_id if user_id is not None else _default_user_id()
    if uid is None:
        return _err("user_id is required when DEFAULT_USER_ID is not set")
    try:
        task_id = task_service.create_task(
            title=title,
            description=description,
            user_id=uid,
            project_id=project_id,
            priority=priority,
            status=status,
        )
        return _ok(f"Created task id={task_id}: {title}")
    except Exception as e:
        return _err(str(e))


@tool
def list_tasks(
    project_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
) -> str:
    """List tasks, optionally filtered by project, user, or status.

    Use this to show the user their tasks or tasks in a project.

    Args:
        project_id: Filter by project ID (optional).
        user_id: Filter by user ID (optional; uses DEFAULT_USER_ID if not set).
        status: Filter by status: pending, in_progress, completed, failed, waiting_for_human (optional).
    """
    uid = user_id if user_id is not None else _default_user_id()
    try:
        tasks = task_service.list_tasks(project_id=project_id, user_id=uid, status=status)
        if not tasks:
            return _ok("No tasks found.")
        lines = [
            f"id={t.id} | {t.title} | status={t.status.value} | priority={t.priority.value}"
            for t in tasks
        ]
        return _ok("\n".join(lines))
    except Exception as e:
        return _err(str(e))


@tool
def get_task(task_id: int) -> str:
    """Get details of a single task by ID.

    Args:
        task_id: The task ID.
    """
    try:
        task = task_service.get_task(task_id)
        if task is None:
            return _ok(f"No task with id={task_id}.")
        return _ok(
            f"id={task.id} | title={task.title} | description={task.description} | "
            f"status={task.status.value} | priority={task.priority.value} | "
            f"project_id={task.project_id} | user_id={task.user_id} | "
            f"assignee_id={task.assignee_id}"
        )
    except Exception as e:
        return _err(str(e))


@tool
def update_task_status(task_id: int, status: str) -> str:
    """Update a task's status.

    Use this when the user wants to mark a task as in progress, completed, or failed.

    Args:
        task_id: The task ID.
        status: One of pending, in_progress, completed, failed, waiting_for_human.
    """
    try:
        task_service.update_task(task_id, status=status)
        return _ok(f"Task id={task_id} status updated to {status}.")
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


@tool
def update_task(task_id: int, status: str | None = None, notes: str | None = None) -> str:
    """Update a task's status and/or append research notes to its description.

    Args:
        task_id: The task ID (from the [Your task ID is #N] in your context).
        status: Optional new status: pending, in_progress, completed, failed.
        notes: Optional text to append to the task description (findings, URLs, etc.).
    """
    try:
        task_service.update_task(task_id, status=status, notes=notes)
        return _ok(f"Task id={task_id} updated.")
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


TOOLS = [create_task, list_tasks, get_task, update_task_status, update_task]
