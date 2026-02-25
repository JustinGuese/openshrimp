"""Task tracking plugin for openshrimp.

Thin @tool wrappers over task_service. No DB logic lives here.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from langchain_core.tools import tool

# Ensure src/ is on sys.path when loaded via plugin loader
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import task_service
import telegram_state
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


# Patterns that suggest the agent only planned/suggested but didn't actually execute
_SUGGESTION_PHRASES = (
    "recommend",
    "suggestion",
    "consider ",
    "you may want",
    "you could",
    "you should",
    "here's what to post",
    "here is what to post",
    "proposed content",
    "draft content",
    "suggested content",
    "post the suggested",
    "post this content",
    "here's a draft",
    "here is a draft",
)


def _notes_look_like_suggestions(notes: str) -> bool:
    """Return True if completion notes contain suggestion language indicating
    the agent only planned/drafted but didn't actually execute the task."""
    lower = notes.lower()
    suggestion_count = sum(1 for phrase in _SUGGESTION_PHRASES if phrase in lower)
    # If 2+ suggestion phrases found, it's likely a plan, not an execution report
    return suggestion_count >= 2


@tool
def create_task(
    title: str,
    description: str,
    project_id: int,
    user_id: int | None = None,
    priority: str = "medium",
    status: str = "pending",
    effort: str = "normal",
    scheduled_at: str | None = None,
    repeat_interval_seconds: int | None = None,
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
        effort: One of quick, normal, deep. Default normal.
        scheduled_at: Optional ISO 8601 datetime string when the task should first run.
        repeat_interval_seconds: Optional interval (in seconds) for recurring tasks.
    """
    uid = user_id if user_id is not None else _default_user_id()
    if uid is None:
        return _err("user_id is required when DEFAULT_USER_ID is not set")

    scheduled_dt = None
    if scheduled_at:
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return _err(
                "scheduled_at must be an ISO 8601 datetime string, "
                "e.g. '2026-02-25T09:00:00'."
            )

    try:
        task_id = task_service.create_task(
            title=title,
            description=description,
            user_id=uid,
            project_id=project_id,
            priority=priority,
            status=status,
            effort=effort,
            scheduled_at=scheduled_dt,
            repeat_interval_seconds=repeat_interval_seconds,
        )
        return _ok(f"Created task id={task_id}: {title}")
    except Exception as e:
        return _err(str(e))


@tool
def schedule_followup_task(
    title: str,
    description: str,
    delay_seconds: int,
    priority: str = "medium",
    repeat_interval_seconds: int | None = None,
) -> str:
    """Schedule a follow-up task for the current task's project and user.

    Use this from inside a running task to create a future one-off or recurring
    task. The new task will:
    - belong to the same user and project as the current task
    - inherit the current task's effort level
    - be scheduled to start in `delay_seconds` from now
    - optionally repeat every `repeat_interval_seconds` seconds
    """
    current_task_id = telegram_state.get_task_id()
    if current_task_id is None:
        return _err(
            "schedule_followup_task can only be used from within an active task context."
        )
    parent = task_service.get_task(current_task_id)
    if parent is None:
        return _err(f"No parent task with id={current_task_id} found.")

    scheduled_at = datetime.now() + timedelta(seconds=delay_seconds)
    effort = parent.effort.value if getattr(parent, "effort", None) else "normal"
    agent_user_id = telegram_state.get_agent_user_id()
    assignee_id = agent_user_id if agent_user_id is not None else parent.assignee_id

    try:
        new_task_id = task_service.create_task(
            title=title,
            description=description,
            user_id=parent.user_id,
            project_id=parent.project_id,
            assignee_id=assignee_id,
            priority=priority,
            status="pending",
            effort=effort,
            chat_id=parent.chat_id,
            scheduled_at=scheduled_at,
            repeat_interval_seconds=repeat_interval_seconds,
        )
    except Exception as e:
        return _err(f"Failed to create follow-up task: {e!r}")

    return _ok(
        f"Scheduled follow-up task id={new_task_id} in {delay_seconds} seconds "
        f"(at {scheduled_at.isoformat(timespec='seconds')}).",
        metadata={
            "task_id": new_task_id,
            "scheduled_at": scheduled_at.isoformat(),
            "repeat_interval_seconds": repeat_interval_seconds,
        },
    )


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
        lines = []
        for t in tasks:
            when = ""
            if getattr(t, "scheduled_at", None):
                when = f" | scheduled_at={t.scheduled_at.isoformat(timespec='seconds')}"
            if getattr(t, "repeat_interval_seconds", None):
                when += f" | repeat_every={t.repeat_interval_seconds}s"
            lines.append(
                f"id={t.id} | {t.title} | status={t.status.value} | "
                f"priority={t.priority.value}{when}"
            )
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
        parts = [
            f"id={task.id}",
            f"title={task.title}",
            f"description={task.description}",
            f"status={task.status.value}",
            f"priority={task.priority.value}",
            f"project_id={task.project_id}",
            f"user_id={task.user_id}",
            f"assignee_id={task.assignee_id}",
        ]
        if getattr(task, "scheduled_at", None):
            parts.append(f"scheduled_at={task.scheduled_at.isoformat(timespec='seconds')}")
        if getattr(task, "repeat_interval_seconds", None):
            parts.append(f"repeat_every={task.repeat_interval_seconds}s")
        return _ok(" | ".join(parts))
    except Exception as e:
        return _err(str(e))


@tool
def update_task_status(task_id: int, status: str) -> str:
    """Update a task's status.

    Use this when the user wants to mark a task as in progress, completed, or failed.
    NOTE: Cannot be used for 'completed' or 'failed' — use update_task() with notes instead.

    Args:
        task_id: The task ID.
        status: One of pending, in_progress, waiting_for_human. (Use update_task for completed/failed.)
    """
    if status in ("completed", "failed"):
        return _err(
            f"Cannot set status to '{status}' without a final summary. "
            f"Use update_task(task_id={task_id}, status='{status}', notes='## Result\\n<your thorough summary>') instead."
        )
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
        notes: Text to append to the task description. REQUIRED when status is 'completed' or 'failed'.
    """
    if status in ("completed", "failed") and not (notes and notes.strip()):
        return _err(
            f"Cannot set status to '{status}' without notes. "
            f"You must provide a final summary in the notes parameter (e.g. '## Result\\n<your findings>')."
        )
    if status == "completed" and notes and _notes_look_like_suggestions(notes):
        return _err(
            "Cannot mark as completed: your notes contain suggestions/recommendations "
            "rather than a report of actions you actually performed. "
            "If you only PLANNED or SUGGESTED what to do but did NOT execute it, "
            "mark the task as FAILED with your suggestions in the notes. "
            "Only mark completed if you actually performed the requested action."
        )
    try:
        task_service.update_task(task_id, status=status, notes=notes)
        return _ok(f"Task id={task_id} updated.")
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(str(e))


TOOLS = [create_task, schedule_followup_task, list_tasks, get_task, update_task_status, update_task]
