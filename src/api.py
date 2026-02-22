"""FastAPI REST API for task visualization (CRUD). User and project as parameters."""

import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure src is on path so db.models and api_schemas resolve when run as src.api from repo root
_src = Path(__file__).resolve().parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

_VIS_ROOT = Path(__file__).resolve().parent / "visualization"

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case
from sqlmodel import Session, select

import db
import task_service
from visualization.api_schemas import TaskCreate, TaskRead, TaskUpdate
from models import Priority, Project, Task, TaskStatus, User

DASHBOARD_ADMIN_TOKEN = (os.environ.get("DASHBOARD_ADMIN_TOKEN") or "").strip()

# SQL expression: high=3, medium=2, low=1 for ORDER BY ... DESC
_priority_order_sql = case(
    (Task.priority == Priority.HIGH, 3),
    (Task.priority == Priority.MEDIUM, 2),
    (Task.priority == Priority.LOW, 1),
    else_=2,
)

app = FastAPI(title="openshrimp Tasks API")

templates = Jinja2Templates(directory=str(_VIS_ROOT / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


@app.exception_handler(Exception)
def log_unhandled_exception(request: Request, exc: Exception):
    """Log every unhandled exception so 500s show up in the terminal."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    if isinstance(exc, StarletteHTTPException):
        raise exc
    logger.exception("Unhandled exception for %s %s: %s", request.method, request.url.path, exc)
    traceback.print_exc()
    return PlainTextResponse(
        f"Internal Server Error\n\n{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
        status_code=500,
    )


def get_session():
    """Dependency that yields a DB session. Routes commit when mutating; session is closed on exit."""
    session = Session(db.get_engine())
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_dashboard_auth(request: Request) -> tuple[int | None, str]:
    """Require token in query; return (scoped_user_id, token). None user_id = admin (unscoped). Raises 403 if missing/invalid."""
    token = (request.query_params.get("token") or "").strip()
    if not token:
        raise HTTPException(status_code=403, detail="Forbidden: missing token")
    if DASHBOARD_ADMIN_TOKEN and token == DASHBOARD_ADMIN_TOKEN:
        return (None, token)
    user_id = task_service.get_user_by_token(token)
    if user_id is None:
        raise HTTPException(status_code=403, detail="Forbidden: invalid token")
    return (user_id, token)


def _task_to_read(task: Task) -> TaskRead:
    return TaskRead(
        id=task.id,
        title=task.title,
        user_id=task.user_id,
        project_id=task.project_id,
        description=task.description,
        status=task.status,
        priority=task.priority,
        assignee_id=task.assignee_id,
        pending_question=task.pending_question,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@app.get("/tasks", response_model=list[TaskRead])
def list_tasks(
    project_id: int | None = Query(None, description="Filter by project; omit for all"),
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> list[TaskRead]:
    """List tasks for the token's user (or all if admin), optionally filtered by project. Sorted by priority desc (high first), then id."""
    scoped_user_id, _ = auth
    q = select(Task)
    if scoped_user_id is not None:
        q = q.where(Task.user_id == scoped_user_id)
    if project_id is not None:
        q = q.where(Task.project_id == project_id)
    q = q.order_by(_priority_order_sql.desc(), Task.id)
    tasks = list(session.exec(q))
    return [_task_to_read(t) for t in tasks]


@app.get("/tasks/{task_id}", response_model=TaskRead)
def get_task(
    task_id: int,
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> TaskRead:
    """Get a single task by ID. Task must belong to token's user unless admin."""
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scoped_user_id, _ = auth
    if scoped_user_id is not None and task.user_id != scoped_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return _task_to_read(task)


@app.post("/tasks", response_model=TaskRead, status_code=201)
def create_task(
    body: TaskCreate,
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> TaskRead:
    """Create a new task. When not admin, user_id is forced to token's user."""
    scoped_user_id, _ = auth
    user_id = scoped_user_id if scoped_user_id is not None else body.user_id
    task = Task(
        title=body.title,
        description=body.description,
        project_id=body.project_id,
        user_id=user_id,
        priority=body.priority,
        status=body.status,
        assignee_id=getattr(body, "assignee_id", None),
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_to_read(task)


@app.patch("/tasks/{task_id}", response_model=TaskRead)
def update_task(
    task_id: int,
    body: TaskUpdate,
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> TaskRead:
    """Update a task (partial). Task must belong to token's user unless admin."""
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scoped_user_id, _ = auth
    if scoped_user_id is not None and task.user_id != scoped_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = body.model_dump(exclude_unset=True)
    if "title" in data:
        task.title = data["title"]
    if "description" in data:
        task.description = data["description"]
    if "project_id" in data:
        task.project_id = data["project_id"]
    if "status" in data:
        task.status = data["status"]
    if "priority" in data:
        task.priority = data["priority"]
    if "assignee_id" in data:
        task.assignee_id = data["assignee_id"]
    task.updated_at = datetime.now()
    session.add(task)
    session.commit()
    session.refresh(task)
    return _task_to_read(task)


@app.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    task_id: int,
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> None:
    """Delete a task. Task must belong to token's user unless admin."""
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    scoped_user_id, _ = auth
    if scoped_user_id is not None and task.user_id != scoped_user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    session.delete(task)
    session.commit()


# --- Dashboard (HTML) and supporting API for dropdowns ---

DEFAULT_USER_ID = 1
OPENSHRIMP_USER_NAME = "openshrimp"


@app.get("/projects")
def list_projects(
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> list[dict]:
    """List projects for the token's user (or all if admin). Used by dashboard dropdown."""
    scoped_user_id, _ = auth
    q = select(Project).order_by(Project.id)
    if scoped_user_id is not None:
        q = q.where(Project.user_id == scoped_user_id)
    projects = list(session.exec(q))
    return [{"id": p.id, "name": p.name, "description": p.description} for p in projects]


@app.get("/assignee-options")
def list_assignee_options(
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> list[dict]:
    """List users for assignee dropdown: token's user (or default when admin) and openshrimp user."""
    scoped_user_id, _ = auth
    current_user_id = scoped_user_id if scoped_user_id is not None else DEFAULT_USER_ID
    current = session.get(User, current_user_id)
    q = select(User).where(User.name == OPENSHRIMP_USER_NAME)
    openshrimp = session.exec(q).first()
    options = []
    if current:
        options.append({"id": current.id, "name": "Current user"})
    if openshrimp and openshrimp.id != current_user_id:
        options.append({"id": openshrimp.id, "name": "openshrimp user"})
    return options


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    project_id: int | None = Query(None, description="Selected project"),
    session: Session = Depends(get_session),
    auth: tuple[int | None, str] = Depends(get_dashboard_auth),
) -> HTMLResponse:
    """Render dashboard: project dropdown, Trello board, tasks table. Scoped to token's user unless admin."""
    try:
        scoped_user_id, token = auth
        default_user_id = scoped_user_id if scoped_user_id is not None else DEFAULT_USER_ID

        q_projects = select(Project).order_by(Project.id)
        if scoped_user_id is not None:
            q_projects = q_projects.where(Project.user_id == scoped_user_id)
        projects = list(session.exec(q_projects))

        current = session.get(User, default_user_id)
        q_openshrimp = select(User).where(User.name == OPENSHRIMP_USER_NAME)
        openshrimp = session.exec(q_openshrimp).first()
        assignee_options = []
        if current:
            assignee_options.append({"id": current.id, "name": "Current user"})
        if openshrimp and openshrimp.id != default_user_id:
            assignee_options.append({"id": openshrimp.id, "name": "openshrimp user"})

        q = select(Task).order_by(_priority_order_sql.desc(), Task.id)
        if scoped_user_id is not None:
            q = q.where(Task.user_id == scoped_user_id)
        if project_id is not None:
            q = q.where(Task.project_id == project_id)
        tasks = [_task_to_read(t) for t in session.exec(q)]
        tasks_by_status = {s.value: [] for s in TaskStatus}
        for t in tasks:
            tasks_by_status[t.status.value].append(t)
        tasks_serialized = [t.model_dump(mode="json") for t in tasks]

        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "token": token,
                "projects": [{"id": p.id, "name": p.name, "description": p.description} for p in projects],
                "assignee_options": assignee_options,
                "tasks": tasks,
                "tasks_by_status": tasks_by_status,
                "tasks_serialized": tasks_serialized,
                "statuses": list(TaskStatus),
                "selected_project_id": project_id,
                "default_user_id": default_user_id,
            },
        )
    except Exception as e:
        logger.exception("Dashboard failed: %s", e)
        traceback.print_exc()
        raise


# Mount static after all routes so middleware stack is correct
app.mount("/static", StaticFiles(directory=str(_VIS_ROOT / "static")), name="static")
