"""Shared task CRUD service.

Business logic for task operations used by both the Telegram bot and the
task_tracking plugin. Plugins are thin @tool wrappers that call these functions.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from sqlmodel import Session, select

import db as _db
from models import DashboardToken, Effort, Priority, Project, Task, TaskStatus, User

logger = logging.getLogger(__name__)

# Sentinel to distinguish "not passed" from "pass None to clear a field"
_UNSET = object()


def _ensure_db() -> None:
    _db.init_db()


# ---------------------------------------------------------------------------
# Dashboard token (secret URL for scoped dashboard access)
# ---------------------------------------------------------------------------


def get_or_create_dashboard_token(user_id: int, chat_id: int) -> str:
    """Return an existing token for this user+chat, or create one with secrets.token_urlsafe(32)."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        existing = session.exec(
            select(DashboardToken).where(
                DashboardToken.user_id == user_id,
                DashboardToken.chat_id == chat_id,
            )
        ).first()
        if existing:
            return existing.token
        token = secrets.token_urlsafe(32)
        row = DashboardToken(token=token, user_id=user_id, chat_id=chat_id)
        session.add(row)
        session.commit()
        return token


def get_user_by_token(token: str) -> int | None:
    """Look up token; return user_id or None. Does not check admin token (API handles that)."""
    if not token or not token.strip():
        return None
    _ensure_db()
    with Session(_db.get_engine()) as session:
        row = session.exec(select(DashboardToken).where(DashboardToken.token == token.strip())).first()
        return row.user_id if row else None


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------


def get_user(user_id: int) -> User | None:
    """Return a User by id, or None if not found."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        return session.get(User, user_id)


def get_or_create_telegram_user(telegram_user_id: int, name: str) -> int:
    """Return DB user_id for a Telegram user, creating one if not found."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        user = session.exec(
            select(User).where(User.telegram_user_id == telegram_user_id)
        ).first()
        if user:
            return user.id
        user = User(
            name=name,
            email=f"tg_{telegram_user_id}@openshrimp.local",
            passwordhash="",
            telegram_user_id=telegram_user_id,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        logger.info(
            "Created DB user id=%s for Telegram user %s (%s).",
            user.id, telegram_user_id, name,
        )
        return user.id


def get_or_create_default_project(user_id: int) -> int:
    """Return the default project_id for a user, creating it if needed."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        project = session.exec(
            select(Project).where(Project.user_id == user_id, Project.name == "Default")
        ).first()
        if project:
            return project.id
        project = Project(
            name="Default",
            user_id=user_id,
            description="Default project",
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        logger.info("Created default project id=%s for user_id=%s.", project.id, user_id)
        return project.id


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_task(
    title: str,
    description: str,
    user_id: int,
    project_id: int,
    assignee_id: int | None = None,
    priority: str = "medium",
    status: str = "pending",
    effort: str = "normal",
    chat_id: int | None = None,
    scheduled_at: datetime | None = None,
    repeat_interval_seconds: int | None = None,
) -> int:
    """Insert a new Task row and return its id."""
    _ensure_db()
    try:
        prio = Priority(priority.lower())
    except ValueError:
        prio = Priority.MEDIUM
    try:
        st = TaskStatus(status.lower())
    except ValueError:
        st = TaskStatus.PENDING
    try:
        eff = Effort(effort.lower())
    except ValueError:
        eff = Effort.NORMAL

    with Session(_db.get_engine()) as session:
        task = Task(
            title=title,
            description=description,
            user_id=user_id,
            project_id=project_id,
            assignee_id=assignee_id,
            priority=prio,
            status=st,
            effort=eff,
            chat_id=chat_id,
            scheduled_at=scheduled_at,
            repeat_interval_seconds=repeat_interval_seconds,
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        return task.id


def get_task(task_id: int) -> Task | None:
    """Return a Task by id, or None if not found."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        return session.get(Task, task_id)


def get_project(project_id: int) -> Project | None:
    """Return a Project by id, or None if not found."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        return session.get(Project, project_id)


def list_projects(user_id: int | None = None) -> list[Project]:
    """Return all projects ordered by id, optionally filtered by user_id."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        q = select(Project).order_by(Project.id)
        if user_id is not None:
            q = q.where(Project.user_id == user_id)
        return list(session.exec(q))


def create_project(name: str, description: str, user_id: int) -> Project:
    """Create a new project and return it (with id populated)."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        project = Project(name=name, user_id=user_id, description=description)
        session.add(project)
        session.commit()
        session.refresh(project)
        logger.info("Created project '%s' id=%s.", name, project.id)
        return project


def list_tasks(
    project_id: int | None = None,
    user_id: int | None = None,
    status: str | None = None,
    assignee_id: int | None = _UNSET,  # type: ignore[assignment]
    scheduled_before: datetime | None = None,
) -> list[Task]:
    """Return tasks, optionally filtered by project, user, status, or assignee."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        q = select(Task)
        if project_id is not None:
            q = q.where(Task.project_id == project_id)
        if user_id is not None:
            q = q.where(Task.user_id == user_id)
        if status is not None:
            try:
                st = TaskStatus(status.lower())
                q = q.where(Task.status == st)
            except ValueError:
                pass
        if assignee_id is not _UNSET:
            q = q.where(Task.assignee_id == assignee_id)
        if scheduled_before is not None:
            from sqlalchemy import or_

            q = q.where(
                or_(
                    Task.scheduled_at == None,  # noqa: E711
                    Task.scheduled_at <= scheduled_before,
                )
            )
        q = q.order_by(Task.id)
        return list(session.exec(q))


def update_task(
    task_id: int,
    status: str | None = None,
    notes: str | None = None,
    assignee_id: object = _UNSET,
    pending_question: object = _UNSET,
    worker_id: object = _UNSET,
) -> Task:
    """Update a task's status, notes, assignee, pending_question, or worker_id.

    Pass ``assignee_id=None``, ``pending_question=None``, or ``worker_id=None``
    to explicitly clear those fields. Omit them (leave as _UNSET) to leave unchanged.
    """
    _ensure_db()
    with Session(_db.get_engine()) as session:
        task = session.get(Task, task_id)
        if task is None:
            raise ValueError(f"No task with id={task_id}")
        old_status = task.status
        if status is not None:
            try:
                task.status = TaskStatus(status.lower())
            except ValueError:
                raise ValueError(f"Invalid status: {status}")
        if notes:
            task.description = (task.description or "") + f"\n\n--- Update ---\n{notes}"
        if assignee_id is not _UNSET:
            task.assignee_id = assignee_id  # type: ignore[assignment]
        if pending_question is not _UNSET:
            task.pending_question = pending_question  # type: ignore[assignment]
        if worker_id is not _UNSET:
            task.worker_id = worker_id  # type: ignore[assignment]

        # If this task just transitioned to COMPLETED and has a repeat interval,
        # enqueue the next occurrence as a new pending task scheduled in the future.
        next_task: Task | None = None
        if (
            old_status != TaskStatus.COMPLETED
            and task.status == TaskStatus.COMPLETED
            and task.repeat_interval_seconds
            and task.repeat_interval_seconds > 0
        ):
            from datetime import timedelta

            scheduled_at = datetime.now() + timedelta(seconds=task.repeat_interval_seconds)
            next_task = Task(
                title=task.title,
                description=task.description,
                user_id=task.user_id,
                project_id=task.project_id,
                assignee_id=task.assignee_id,
                priority=task.priority,
                status=TaskStatus.PENDING,
                pending_question=None,
                effort=task.effort,
                chat_id=task.chat_id,
                worker_id=None,
                heartbeat_at=None,
                scheduled_at=scheduled_at,
                repeat_interval_seconds=task.repeat_interval_seconds,
            )

        task.updated_at = datetime.now()
        session.add(task)
        if next_task is not None:
            session.add(next_task)
        session.commit()
        session.refresh(task)
        return task


def reset_waiting_tasks() -> int:
    """Set all waiting_for_human tasks to failed. Call on bot startup.

    Returns the number of tasks reset.
    """
    _ensure_db()
    with Session(_db.get_engine()) as session:
        q = select(Task).where(Task.status == TaskStatus.WAITING_FOR_HUMAN)
        tasks = list(session.exec(q))
        for task in tasks:
            task.status = TaskStatus.FAILED
            task.assignee_id = None
            task.pending_question = None
            session.add(task)
        session.commit()
        return len(tasks)


def update_heartbeat(task_id: int) -> None:
    """Update heartbeat_at for an IN_PROGRESS task to prove it is still alive."""
    _ensure_db()
    with Session(_db.get_engine()) as session:
        task = session.get(Task, task_id)
        if task is not None and task.status == TaskStatus.IN_PROGRESS:
            task.heartbeat_at = datetime.now()
            task.updated_at = datetime.now()
            session.add(task)
            session.commit()


def reset_stale_in_progress(timeout_minutes: int = 10) -> int:
    """Reset IN_PROGRESS tasks that have not sent a heartbeat within *timeout_minutes*.

    Tasks are reset to PENDING so they can be retried. Returns the count reset.
    """
    from datetime import timedelta
    from sqlalchemy import or_
    _ensure_db()
    cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
    with Session(_db.get_engine()) as session:
        q = select(Task).where(
            Task.status == TaskStatus.IN_PROGRESS,
        ).where(
            or_(
                Task.heartbeat_at < cutoff,
                (Task.heartbeat_at == None) & (Task.updated_at < cutoff),  # noqa: E711
            )
        )
        tasks = list(session.exec(q))
        for task in tasks:
            logger.warning(
                "Resetting stale IN_PROGRESS task #%s '%s' (last heartbeat=%s)",
                task.id, task.title, task.heartbeat_at,
            )
            task.status = TaskStatus.PENDING
            task.worker_id = None
            task.heartbeat_at = None
            task.updated_at = datetime.now()
            session.add(task)
        session.commit()
        return len(tasks)


def reset_foreign_workers(current_worker_id: str) -> int:
    """Reset IN_PROGRESS tasks owned by a different worker (previous process).

    Resets them to PENDING so the current process can pick them up.
    Returns the count reset.
    """
    from sqlalchemy import or_
    _ensure_db()
    with Session(_db.get_engine()) as session:
        q = select(Task).where(
            Task.status == TaskStatus.IN_PROGRESS,
            or_(
                Task.worker_id != current_worker_id,
                Task.worker_id == None,  # noqa: E711
            ),
        )
        tasks = list(session.exec(q))
        for task in tasks:
            logger.warning(
                "Resetting IN_PROGRESS task #%s '%s' from old worker '%s'.",
                task.id, task.title, task.worker_id,
            )
            task.status = TaskStatus.PENDING
            task.worker_id = None
            task.heartbeat_at = None
            task.updated_at = datetime.now()
            session.add(task)
        session.commit()
        return len(tasks)


def list_active_tasks(user_id: int | None = None) -> list[tuple[Task, Project]]:
    """Return active tasks (pending, in progress, waiting) with their project.

    Order: by status (PENDING, IN_PROGRESS, WAITING_FOR_HUMAN) then by task id.
    If user_id is set, only tasks belonging to that user are returned (for scoped views).
    """
    try:
        statuses = [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_HUMAN]
        _ensure_db()
        with Session(_db.get_engine()) as session:
            q = (
                select(Task, Project)
                .join(Project, Task.project_id == Project.id)
                .where(Task.status.in_(statuses))  # type: ignore[attr-defined]
                .order_by(Task.id)
            )
            if user_id is not None:
                q = q.where(Task.user_id == user_id)
            rows = list(session.exec(q))
            # Sort by status order
            order = {s: i for i, s in enumerate(statuses)}
            rows.sort(key=lambda r: (order.get(r[0].status, 99), r[0].id))
            return rows
    except Exception as e:
        logger.warning("Failed to list active tasks: %s", e)
        return []


def list_active_summary(user_id: int | None = None) -> str:
    """Return a human-readable summary of pending/in-progress/waiting tasks.

    Each line shows task id, title, status, and project name.
    Used e.g. in agent context (_build_query). Pass user_id to scope to one user.
    """
    rows = list_active_tasks(user_id=user_id)
    if not rows:
        return ""
    lines = [f"  #{t.id}: {t.title} [{t.status.value}] — {p.name}" for t, p in rows]
    return "Active tasks:\n" + "\n".join(lines)


# Board section headers (status -> label)
_BOARD_HEADERS = {
    TaskStatus.PENDING: "📋 Pending",
    TaskStatus.IN_PROGRESS: "🔄 In progress",
    TaskStatus.WAITING_FOR_HUMAN: "⏳ Waiting for you",
}


def list_active_summary_board(user_id: int | None = None) -> str:
    """Return a board-style summary: tasks grouped by status (Trello-like columns). Pass user_id to scope to one user."""
    rows = list_active_tasks(user_id=user_id)
    if not rows:
        return ""
    # Group by status preserving order
    sections: dict[TaskStatus, list[tuple[Task, Project]]] = {}
    for t, p in rows:
        sections.setdefault(t.status, []).append((t, p))
    lines = []
    for status in [TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.WAITING_FOR_HUMAN]:
        if status not in sections:
            continue
        lines.append(_BOARD_HEADERS.get(status, status.value))
        for t, p in sections[status]:
            title = (t.title or "")[:80]
            if len(t.title or "") > 80:
                title += "…"
            lines.append(f"  • #{t.id}: {title}")
        lines.append("")  # blank between sections
    return "\n".join(lines).rstrip()


def list_active_summary_table(user_id: int | None = None) -> str:
    """Return a monospace table: ID | Status | Title | Project.

    Truncates title and status for narrow screens. Caller should wrap in <pre> and
    escape for HTML when sending with parse_mode=HTML. Pass user_id to scope to one user.
    """
    rows = list_active_tasks(user_id=user_id)
    if not rows:
        return ""
    TITLE_W = 24
    STATUS_W = 10
    PROJECT_W = 12
    id_w = 3
    lines = []
    header = f"{'ID':>{id_w}} │ {'Status':<{STATUS_W}} │ {'Title':<{TITLE_W}} │ {'Project':<{PROJECT_W}}"
    sep = f"{'─' * id_w}─┼─{'─' * STATUS_W}─┼─{'─' * TITLE_W}─┼─{'─' * PROJECT_W}"
    lines.append(header)
    lines.append(sep)
    def _cell(text: str, w: int) -> str:
        s = (text or "").replace("\n", " ")[:w]
        if len(text or "") > w:
            s = (s[: w - 1] + "…")[:w]
        return s.ljust(w)[:w]

    for t, p in rows:
        status = _cell(t.status.value or "", STATUS_W)
        title = _cell(t.title or "", TITLE_W)
        project = _cell(p.name or "", PROJECT_W)
        lines.append(f"{t.id:>{id_w}} │ {status} │ {title} │ {project}")
    return "\n".join(lines)
