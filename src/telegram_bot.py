"""Telegram bot frontend for openshrimp agent.

Polls for messages, creates a DB task for each request, runs the research agent
in a thread pool, and sends live progress updates back to the user.

Usage:
    uv run python src/telegram_bot.py

Required env vars:
    TELEGRAM_BOT_TOKEN       — from BotFather
    DEFAULT_USER_ID          — DB user id for the human (default: auto-created)
    DEFAULT_PROJECT_ID       — DB project id for created tasks (default: auto-created)
    DEFAULT_AGENT_USER_ID    — DB user id for the agent (default: auto-created)
"""

import asyncio
import functools
import html
import logging
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

# Ensure src/ is on sys.path when run directly
_src = Path(__file__).resolve().parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from sqlmodel import Session, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

import db as _db
import human_input as _human_input
import task_service
import telegram_state
from agent import run_research
from models import Project, TaskStatus, User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("telegram_bot")

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-worker")

# Unique identifier for this bot process — used to claim tasks and detect orphans from old processes
WORKER_ID = f"tg-{uuid.uuid4().hex[:8]}"

DEFAULT_USER_ID = int(os.environ.get("DEFAULT_USER_ID", "0"))
DEFAULT_PROJECT_ID = int(os.environ.get("DEFAULT_PROJECT_ID", "0"))
DEFAULT_AGENT_USER_ID = int(os.environ.get("DEFAULT_AGENT_USER_ID", "0"))

# Resolved at startup by _ensure_default_user_and_project()
_human_user_id: int = 0
_agent_user_id: int = 0
_effective_project_id: int = 0

AUTO_START_TASKS = os.environ.get("AUTO_START_TASKS", "true").strip().lower() in ("true", "1", "yes")

AGENT_MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "2"))
RETRY_BACKOFF = [30, 60]  # seconds between retries
# Keep max backoff below watchdog so IN_PROGRESS task is not reset during retry sleep
WATCHDOG_TIMEOUT_MINUTES = max(1, int(os.environ.get("HEARTBEAT_WATCHDOG_MINUTES", "10")))
MAX_RETRY_BACKOFF_SECONDS = 300  # 5 min; must be < WATCHDOG_TIMEOUT_MINUTES * 60


def _is_transient_error(exc: Exception) -> bool:
    """Conservative check for retriable errors."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__
    if name in ("ReadTimeout", "ConnectTimeout", "RemoteProtocolError", "ConnectError"):
        return True
    s = str(exc)
    if any(code in s for code in ("429", "500", "502", "503", "504")):
        return True
    if any(kw in s.lower() for kw in ("timeout", "rate limit", "connection reset")):
        return True
    return False


# Per-chat log level: "INFO" = only answers/follow-ups; "DEBUG" = include tool progress
_chat_log_level: dict[int, str] = {}
DEFAULT_CHAT_LEVEL = "INFO"

# Prefix for all bot-originated messages
BOT_PREFIX = "🦐 "

# Telegram message length limit (API returns 400 if exceeded)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_CHUNK_SIZE = 4090

# Inline keyboard for /status: Refresh button
STATUS_REFRESH_DATA = "status:refresh"

PROJECT_SELECT_PREFIX = "proj:"
PROJECT_CONFIRM_PREFIX = "projconfirm:"
PROJECT_SET_PREFIX = "setproj:"

# Logo path for /start greeting (project root logo.jpg)
_LOGO_PATH = Path(__file__).resolve().parent.parent / "logo.jpg"

# Keywords for effort detection
_QUICK_KEYWORDS = ("quick", "briefly", "brief", "fast", "tl;dr", "tldr", "short", "summary only")
_DEEP_KEYWORDS = ("deep", "comprehensive", "thorough", "exhaustive", "deep dive", "extensive", "in detail", "detailed")


def _detect_effort(text: str) -> str:
    """Detect effort level from message text. Returns 'quick', 'normal', or 'deep'."""
    t = text.lower()
    if any(kw in t for kw in _DEEP_KEYWORDS):
        return "deep"
    if any(kw in t for kw in _QUICK_KEYWORDS):
        return "quick"
    return "normal"


@dataclass
class _ProjectPending:
    user_text: str
    auto_name: str  # proposed new project name
    message_id: int  # bot message with the keyboard; used to reject stale taps
    effort: str = "normal"


_pending_project: dict[int, _ProjectPending] = {}  # keyed by chat_id

# Remember last-used project per chat_id for the quick Yes/No confirmation flow
_last_project: dict[int, int] = {}  # chat_id → project_id


def _resolve_project(project_id: int | None) -> tuple[int, str] | None:
    """Return (id, name) if project exists, else None."""
    if project_id is None:
        return None
    project = task_service.get_project(project_id)
    if project is None:
        return None
    return (project.id, project.name)


def _chunk_text(text: str, max_len: int = TELEGRAM_CHUNK_SIZE) -> list[str]:
    """Split text into chunks at or under max_len, preferring newline boundaries."""
    if len(text) <= max_len:
        return [text] if text else []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        segment = rest[: max_len + 1]
        last_nl = segment.rfind("\n")
        if last_nl > max_len // 2:
            split = last_nl + 1
        else:
            split = max_len
        chunks.append(rest[:split].rstrip())
        rest = rest[split:].lstrip()
    return chunks


def _is_telegram_valid_url(url: str) -> bool:
    """Telegram rejects localhost/127.0.0.1 in inline keyboard URLs; only public URLs are allowed."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return host not in ("localhost", "127.0.0.1", "::1")
    except Exception:
        return False


def _status_keyboard(dashboard_url: str | None = None) -> InlineKeyboardMarkup:
    """Inline keyboard for the status message: Refresh button, and optionally an Open dashboard URL button."""
    buttons = [InlineKeyboardButton("🔄 Refresh", callback_data=STATUS_REFRESH_DATA)]
    if dashboard_url and _is_telegram_valid_url(dashboard_url):
        buttons.append(InlineKeyboardButton("📊 Open dashboard", url=dashboard_url))
    return InlineKeyboardMarkup.from_row(buttons)


def _propose_project_name(user_text: str) -> str:
    """Derive a short title-cased project name from the query."""
    name = user_text.strip().rstrip("?.!").strip()
    if len(name) > 40:
        # truncate at last word boundary
        name = name[:40].rsplit(" ", 1)[0]
    return name.title()


def _build_project_keyboard(
    projects: list, auto_name: str, default_project_id: int
) -> InlineKeyboardMarkup:
    """Inline keyboard: existing projects + New option. Max 5 projects shown."""
    rows = []
    for proj in projects[:5]:
        label = proj.name if proj.id != default_project_id else f"{proj.name} (default)"
        rows.append([InlineKeyboardButton(f"📁 {label}", callback_data=f"proj:id:{proj.id}")])
    short_name = auto_name[:35]
    rows.append([InlineKeyboardButton(f'➕ New: "{short_name}"', callback_data="proj:new")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Startup: ensure DB objects exist
# ---------------------------------------------------------------------------


def _get_or_create_user(session: Session, user_id: int, name: str, email: str) -> User:
    """Return existing user by id, then by email, or create one."""
    if user_id:
        user = session.get(User, user_id)
        if user:
            return user
    # Fall back to lookup by email so restarts don't create duplicate users
    existing = session.exec(select(User).where(User.email == email)).first()
    if existing:
        return existing
    user = User(name=name, email=email, passwordhash="")
    session.add(user)
    session.commit()
    session.refresh(user)
    logger.info("Created user '%s' id=%s.", name, user.id)
    return user


def _ensure_default_user_and_project() -> None:
    """Create human user, agent user, and default project on startup if missing."""
    global _human_user_id, _agent_user_id, _effective_project_id
    _db.init_db()
    with Session(_db.get_engine()) as session:
        human = _get_or_create_user(
            session, DEFAULT_USER_ID, "Telegram", "telegram@openshrimp.local"
        )
        _human_user_id = human.id

        agent = _get_or_create_user(
            session, DEFAULT_AGENT_USER_ID, "openshrimp", "agent@openshrimp.local"
        )
        _agent_user_id = agent.id

        project_id = DEFAULT_PROJECT_ID
        if project_id:
            project = session.get(Project, project_id)
        else:
            project = None
        if project is None:
            project = Project(
                name="Default",
                user_id=_human_user_id,
                description="Default project for Telegram tasks",
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            logger.info("Created default project id=%s.", project.id)
        _effective_project_id = project.id

    logger.info(
        "Startup: human_user_id=%s agent_user_id=%s project_id=%s",
        _human_user_id,
        _agent_user_id,
        _effective_project_id,
    )


def _reset_orphaned_tasks() -> None:
    """On startup: reset waiting_for_human tasks to failed and reset IN_PROGRESS tasks from previous processes."""
    count = task_service.reset_waiting_tasks()
    if count:
        logger.warning("Reset %s orphaned waiting_for_human task(s) to failed.", count)
    foreign = task_service.reset_foreign_workers(current_worker_id=WORKER_ID)
    if foreign:
        logger.warning("Reset %s IN_PROGRESS task(s) from previous processes to pending.", foreign)


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------


def _build_query(user_text: str, task_id: int, active_ctx: str) -> str:
    prefix = f"[Your task ID is #{task_id}]"
    if active_ctx:
        prefix += f"\n{active_ctx}"
    return f"{prefix}\n\n{user_text}"


# ---------------------------------------------------------------------------
# Progress callback (runs in worker thread, sends message to async loop)
# ---------------------------------------------------------------------------


def _on_progress(
    tool_name: str,
    args: dict,
    observation: str,
    app: Application,
    chat_id: int,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Send a brief progress message after each tool execution (DEBUG level only)."""
    if _chat_log_level.get(chat_id, DEFAULT_CHAT_LEVEL) != "DEBUG":
        return
    preview = observation[:120].replace("\n", " ") if observation else ""
    text = BOT_PREFIX + (f"🔧 *{tool_name}* → {preview}…" if preview else f"🔧 *{tool_name}* called")
    try:
        asyncio.run_coroutine_threadsafe(
            app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
            loop,
        ).result(timeout=10)
    except Exception as e:
        logger.warning("Failed to send progress message: %s", e)


# ---------------------------------------------------------------------------
# Worker thread: runs the agent synchronously
# ---------------------------------------------------------------------------


def _run_agent_in_thread(
    query: str,
    chat_id: int,
    task_id: int,
    task_title: str,
    app: Application,
    loop: asyncio.AbstractEventLoop,
    effort: str = "normal",
) -> None:
    """Execute the research agent and send the final answer back to the user."""
    telegram_state.set_context(
        chat_id=chat_id,
        bot_app=app,
        loop=loop,
        task_id=task_id,
        human_user_id=_human_user_id,
        agent_user_id=_agent_user_id,
    )
    try:
        task_service.update_task(
            task_id,
            status=TaskStatus.IN_PROGRESS.value,
            assignee_id=_agent_user_id,
            worker_id=WORKER_ID,
        )
    except Exception as e:
        logger.warning("Failed to mark task %s in_progress: %s", task_id, e)

    def on_progress(tool_name: str, args: dict, observation: str) -> None:
        _on_progress(tool_name, args, observation, app, chat_id, loop)

    failed = False
    body = ""
    max_attempts = 1 + AGENT_MAX_RETRIES
    for attempt in range(max_attempts):
        try:
            body = run_research(query, on_progress=on_progress, effort=effort) or "(No answer produced)"
            task_service.update_task(task_id, status=TaskStatus.COMPLETED.value)
            break
        except Exception as e:
            logger.exception("Agent failed for task %s: %s", task_id, e)
            body = f"❌ Research failed: {e!r}"
            retries_left = max_attempts - 1 - attempt
            if _is_transient_error(e) and retries_left > 0:
                raw_backoff = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                backoff = min(raw_backoff, MAX_RETRY_BACKOFF_SECONDS)
                logger.warning(
                    "Retrying task %s in %ss (attempt %s/%s)",
                    task_id, backoff, attempt + 1, max_attempts,
                )
                try:
                    asyncio.run_coroutine_threadsafe(
                        app.bot.send_message(
                            chat_id=chat_id,
                            text=BOT_PREFIX + f"⏳ Retrying in {backoff}s…",
                        ),
                        loop,
                    ).result(timeout=30)
                except Exception:
                    pass
                time.sleep(backoff)
            else:
                failed = True
                try:
                    task_service.update_task(task_id, status=TaskStatus.FAILED.value)
                except Exception:
                    pass
                break

    if failed:
        answer = BOT_PREFIX + f"❌ Task #{task_id} failed:\n\n{body}\n\n---\nTask: {task_title}"
    else:
        answer = BOT_PREFIX + f"✅ Task #{task_id} completed:\n\n{body}\n\n---\nTask: {task_title}"

    try:
        chunks = _chunk_text(answer)
        for chunk in chunks:
            asyncio.run_coroutine_threadsafe(
                app.bot.send_message(chat_id=chat_id, text=chunk),
                loop,
            ).result(timeout=30)
    except Exception as e:
        logger.error("Failed to send final answer to chat %s: %s", chat_id, e)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context) -> None:
    greeting = (
        BOT_PREFIX
        + "Welcome to openShrimp — your autonomous research agent.\n\n"
        "Send me any question or research task and I'll work on it until it's done. "
        "No babysitting required — I'll ask you directly if I need clarification.\n\n"
        "Commands:\n"
        "/status — see active tasks + dashboard link\n"
        "/dashboard — get your personal task dashboard\n"
        "/project — switch default project for new tasks\n"
        "/loglevel — toggle verbosity (INFO/DEBUG)\n\n"
        "Just type your question to get started.\n\n"
        "For paid premium hosted plans (larger models, higher limits), "
        "contact info@datafortress.cloud."
    )
    if _LOGO_PATH.is_file():
        try:
            with open(_LOGO_PATH, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=greeting,
                )
        except Exception as e:
            logger.warning("Failed to send logo on /start: %s", e)
            await update.message.reply_text(greeting)
    else:
        await update.message.reply_text(greeting)


async def cmd_dashboard(update: Update, context) -> None:
    """Send the user a secret URL to their scoped dashboard."""
    chat_id = update.effective_chat.id
    url = _dashboard_url_for_chat(chat_id)
    if _is_telegram_valid_url(url):
        keyboard = InlineKeyboardMarkup.from_row([
            InlineKeyboardButton("📊 Open dashboard", url=url),
        ])
        await update.message.reply_text(
            BOT_PREFIX + "Your dashboard (tap the button to open):",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            BOT_PREFIX + "Your dashboard (copy link to open in browser):\n" + url,
        )


def _dashboard_url_for_chat(chat_id: int) -> str:
    """Build the scoped dashboard URL for the current human user and chat."""
    base_url = (os.environ.get("DASHBOARD_BASE_URL", "http://localhost:8000") or "http://localhost:8000").rstrip("/")
    token = task_service.get_or_create_dashboard_token(_human_user_id, chat_id)
    return f"{base_url}/?token={token}"


async def cmd_status(update: Update, context) -> None:
    project = task_service.get_project(_effective_project_id)
    use_table = context.args and context.args[0].lower() == "table"
    chat_id = update.effective_chat.id
    dashboard_url = _dashboard_url_for_chat(chat_id)

    if project:
        current_project_line_md = f"Current project: *{project.name}*\n\n"
        current_project_line_html = f"Current project: <b>{html.escape(project.name)}</b>\n\n"
    else:
        current_project_line_md = ""
        current_project_line_html = ""

    dashboard_line_md = ""
    dashboard_line_html = ""

    keyboard = _status_keyboard(dashboard_url)
    if use_table:
        table_text = task_service.list_active_summary_table(user_id=_human_user_id)
        if table_text:
            escaped = html.escape(table_text)
            body = BOT_PREFIX + current_project_line_html + f"<pre>{escaped}</pre>" + dashboard_line_html
            await update.message.reply_text(body, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.message.reply_text(
                BOT_PREFIX + current_project_line_html + "✅ No active tasks right now." + dashboard_line_html,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
    else:
        summary = task_service.list_active_summary_board(user_id=_human_user_id)
        if summary:
            await update.message.reply_text(
                BOT_PREFIX + current_project_line_md + summary + dashboard_line_md,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await update.message.reply_text(
                BOT_PREFIX + current_project_line_md + "✅ No active tasks right now." + dashboard_line_md,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )


async def on_status_refresh(update: Update, context) -> None:
    """Handle Refresh button on status message: update the message with current board."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    dashboard_url = _dashboard_url_for_chat(chat_id)

    project = task_service.get_project(_effective_project_id)
    current_project_line = (
        f"Current project: *{project.name}*\n\n" if project else ""
    )
    summary = task_service.list_active_summary_board(user_id=_human_user_id)
    if summary:
        text = BOT_PREFIX + current_project_line + summary
    else:
        text = BOT_PREFIX + current_project_line + "✅ No active tasks right now."

    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=_status_keyboard(dashboard_url),
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.warning("Failed to edit status message: %s", e)


async def cmd_loglevel(update: Update, context) -> None:
    """Set message verbosity: INFO = only answers/follow-ups; DEBUG = include tool progress."""
    chat_id = update.effective_chat.id
    raw = context.args or []
    if not raw:
        level = _chat_log_level.get(chat_id, DEFAULT_CHAT_LEVEL)
        await update.message.reply_text(
            BOT_PREFIX
            + f"📋 Log level: *{level}*\n\n"
            "• *INFO* — only my answers and follow-up questions\n"
            "• *DEBUG* — also tool progress (e.g. browser, telegram)\n\n"
            "Use: `/loglevel INFO` or `/loglevel DEBUG`",
            parse_mode="Markdown",
        )
        return
    level = raw[0].upper()
    if level not in ("INFO", "DEBUG"):
        await update.message.reply_text(
            BOT_PREFIX + "Use `/loglevel INFO` or `/loglevel DEBUG`.",
            parse_mode="Markdown",
        )
        return
    _chat_log_level[chat_id] = level
    await update.message.reply_text(
        BOT_PREFIX + f"✅ Log level set to *{level}*.", parse_mode="Markdown"
    )


async def cmd_project(update: Update, context) -> None:
    """Show project list so user can set the default project for new tasks."""
    projects = task_service.list_projects(user_id=_human_user_id)
    if not projects:
        await update.message.reply_text(BOT_PREFIX + "No projects yet. Send a task to create one.")
        return
    rows = [
        [InlineKeyboardButton(f"📁 {p.name}", callback_data=f"{PROJECT_SET_PREFIX}{p.id}")]
        for p in projects[:10]
    ]
    await update.message.reply_text(
        BOT_PREFIX + "Select project for new tasks:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_set_project(update: Update, context) -> None:
    """Handle project selection from /project keyboard."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    # callback_data is "setproj:<id>"
    data = query.data or ""
    if not data.startswith(PROJECT_SET_PREFIX):
        return
    try:
        project_id = int(data[len(PROJECT_SET_PREFIX) :])
    except ValueError:
        await query.edit_message_text(BOT_PREFIX + "⚠️ Invalid project.")
        return
    project = task_service.get_project(project_id)
    if project is None:
        await query.edit_message_text(BOT_PREFIX + "⚠️ Project no longer exists.")
        return
    _last_project[chat_id] = project_id
    await query.edit_message_text(
        BOT_PREFIX + f"✅ Project set to *{project.name}*. New tasks will use this project.",
        parse_mode="Markdown",
    )


EFFORT_LABEL = {"quick": " ⚡ quick", "deep": " 🔬 deep", "normal": ""}


async def _create_task_and_launch_agent(
    chat_id: int,
    project_id: int,
    project_name: str,
    user_text: str,
    effort: str,
    app: Application,
    loop: asyncio.AbstractEventLoop,
    send_start_message: Callable[[str], Awaitable[None]],
    send_error_message: Callable[[str], Awaitable[None]],
) -> None:
    """Create a DB task and launch the agent. Caller provides how to send start/error messages."""
    _last_project[chat_id] = project_id

    task_title = user_text[:120]
    try:
        task_id = task_service.create_task(
            title=task_title,
            description=user_text,
            user_id=_human_user_id,
            project_id=project_id,
            assignee_id=_agent_user_id,
            effort=effort,
        )
    except Exception as e:
        logger.exception("Failed to create task in DB: %s", e)
        await send_error_message(BOT_PREFIX + f"⚠️ Could not create task (DB error): {e}")
        return

    active_ctx = task_service.list_active_summary(user_id=_human_user_id)
    research_query = _build_query(user_text, task_id, active_ctx)

    effort_label = EFFORT_LABEL.get(effort, "")
    start_text = BOT_PREFIX + f"⏳ Starting task #{task_id}{effort_label} in project *{project_name}*…"
    await send_start_message(start_text)

    loop.run_in_executor(
        _executor,
        functools.partial(
            _run_agent_in_thread,
            query=research_query,
            chat_id=chat_id,
            task_id=task_id,
            task_title=task_title,
            app=app,
            loop=loop,
            effort=effort,
        ),
    )


async def _create_and_start_task(
    edit_target,  # object with .edit_message_text() — query or message
    chat_id: int,
    project_id: int,
    project_name: str,
    user_text: str,
    effort: str,
    app: Application,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Create a DB task and launch the agent worker. Shared by project selection handlers."""
    await _create_task_and_launch_agent(
        chat_id, project_id, project_name, user_text, effort, app, loop,
        send_start_message=lambda t: edit_target.edit_message_text(t, parse_mode="Markdown"),
        send_error_message=lambda t: edit_target.edit_message_text(t),
    )


async def _auto_start_task(
    update: Update,
    chat_id: int,
    project_id: int,
    project_name: str,
    user_text: str,
    effort: str,
    app: Application,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Create a DB task and launch the agent; no existing bot message to edit (use reply_text)."""
    await _create_task_and_launch_agent(
        chat_id, project_id, project_name, user_text, effort, app, loop,
        send_start_message=lambda t: update.message.reply_text(t, parse_mode="Markdown"),
        send_error_message=lambda t: update.message.reply_text(t),
    )


async def on_project_selected(update: Update, context) -> None:
    """Handle project selection button tap (full project picker)."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    pending = _pending_project.get(chat_id)
    if pending is None or pending.message_id != query.message.message_id:
        await query.edit_message_text(BOT_PREFIX + "⚠️ This selection has expired.")
        return
    del _pending_project[chat_id]

    loop = asyncio.get_running_loop()
    app = context.application
    user_text = pending.user_text
    effort = pending.effort
    data = query.data  # "proj:id:N" or "proj:new"

    if data.startswith("proj:id:"):
        project_id = int(data.split(":")[-1])
        project = task_service.get_project(project_id)
        project_name = project.name if project else "Unknown"
    else:  # "proj:new"
        project = task_service.create_project(
            name=pending.auto_name,
            description=f"Created from task: {user_text[:200]}",
            user_id=_human_user_id,
        )
        project_id = project.id
        project_name = project.name

    await _create_and_start_task(query, chat_id, project_id, project_name, user_text, effort, app, loop)


async def on_project_confirm(update: Update, context) -> None:
    """Handle Yes/No confirmation for last-used project.

    Callback data format:
      projconfirm:yes:<project_id>  — use the last project directly
      projconfirm:no                — show the full project picker
    """
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    pending = _pending_project.get(chat_id)
    if pending is None or pending.message_id != query.message.message_id:
        await query.edit_message_text(BOT_PREFIX + "⚠️ This selection has expired.")
        return

    data = query.data  # "projconfirm:yes:<id>" or "projconfirm:no"
    loop = asyncio.get_running_loop()
    app = context.application

    if data.startswith("projconfirm:yes:"):
        project_id = int(data.split(":")[-1])
        project = task_service.get_project(project_id)
        if project is None:
            # Project no longer exists — fall through to full picker
            pass
        else:
            del _pending_project[chat_id]
            await _create_and_start_task(
                query, chat_id, project_id, project.name,
                pending.user_text, pending.effort, app, loop,
            )
            return

    # "projconfirm:no" or project gone — show the full project picker
    del _pending_project[chat_id]
    user_text = pending.user_text
    effort = pending.effort
    auto_name = pending.auto_name
    projects = task_service.list_projects(user_id=_human_user_id)
    keyboard = _build_project_keyboard(projects, auto_name, _effective_project_id)
    result = await query.edit_message_text(
        BOT_PREFIX + "📋 Which project should I file this under?",
        reply_markup=keyboard,
    )
    # edit_message_text returns Message on success, True if message unchanged
    if not hasattr(result, "message_id"):
        logger.warning("edit_message_text returned non-Message; using original message_id")
        message_id = query.message.message_id
    else:
        message_id = result.message_id
    _pending_project[chat_id] = _ProjectPending(
        user_text=user_text, auto_name=auto_name, message_id=message_id, effort=effort
    )


async def on_message(update: Update, context) -> None:
    """Handle a free-form text message.

    If there is a pending ask_human question for this chat, route the reply to
    the waiting agent thread. Otherwise, propose a project and start the task.
    """
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    chat_id = update.effective_chat.id

    # Route reply to waiting agent thread
    if _human_input.has_pending(chat_id):
        _human_input.resolve(chat_id, user_text)
        return

    if AUTO_START_TASKS:
        resolved = _resolve_project(_last_project.get(chat_id)) or _resolve_project(_effective_project_id)
        if resolved:
            effort = _detect_effort(user_text)
            loop = asyncio.get_running_loop()
            app = context.application
            await _auto_start_task(update, chat_id, *resolved, user_text, effort, app, loop)
            return

    effort = _detect_effort(user_text)
    auto_name = _propose_project_name(user_text)

    # If there is a previously used project for this chat, offer a quick Yes/No confirmation
    last_pid = _last_project.get(chat_id)
    if last_pid:
        last_project = task_service.get_project(last_pid)
        if last_project is not None:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"✅ Yes, use '{last_project.name}'",
                        callback_data=f"projconfirm:yes:{last_pid}",
                    ),
                    InlineKeyboardButton("🔀 Pick another", callback_data="projconfirm:no"),
                ]
            ])
            msg = await update.message.reply_text(
                BOT_PREFIX + f"📋 Use project *{last_project.name}* again?",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
            _pending_project[chat_id] = _ProjectPending(
                user_text=user_text, auto_name=auto_name,
                message_id=msg.message_id, effort=effort,
            )
            return

    # No remembered project — show full project selection keyboard
    projects = task_service.list_projects(user_id=_human_user_id)
    keyboard = _build_project_keyboard(projects, auto_name, _effective_project_id)
    msg = await update.message.reply_text(
        BOT_PREFIX + "📋 Which project should I file this under?",
        reply_markup=keyboard,
    )
    _pending_project[chat_id] = _ProjectPending(
        user_text=user_text, auto_name=auto_name, message_id=msg.message_id, effort=effort
    )


# ---------------------------------------------------------------------------
# Periodic reminder job
# ---------------------------------------------------------------------------


async def _send_reminders(context) -> None:
    """Send reminder messages for questions that have been waiting too long."""
    for pq in _human_input.get_stale(stale_seconds=300):
        try:
            await context.bot.send_message(
                chat_id=pq.chat_id,
                text=(
                    BOT_PREFIX
                    + f"⏰ Still waiting for your reply on task #{pq.task_id}:\n{pq.question}"
                ),
            )
        except Exception as e:
            logger.warning("Failed to send reminder to chat %s: %s", pq.chat_id, e)


async def _check_orphaned_tasks(context) -> None:
    """Watchdog: reset IN_PROGRESS tasks that have stopped sending heartbeats."""
    try:
        reset_count = task_service.reset_stale_in_progress(timeout_minutes=WATCHDOG_TIMEOUT_MINUTES)
        if reset_count:
            logger.warning("Watchdog reset %s stale IN_PROGRESS task(s) to pending.", reset_count)
    except Exception as e:
        logger.warning("Watchdog error while checking stale tasks: %s", e)


async def _error_handler(update: object, context) -> None:
    """Log uncaught exceptions from handlers so they don't fail silently."""
    logger.exception("Unhandled exception in handler: %s", context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to .env or export it.")

    _db.init_db()
    _ensure_default_user_and_project()
    _reset_orphaned_tasks()
    logger.info("DB initialized.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("project", cmd_project))
    # projconfirm: must be registered BEFORE proj: to avoid prefix collision
    app.add_handler(CallbackQueryHandler(on_project_confirm, pattern=r"^projconfirm:"))
    app.add_handler(CallbackQueryHandler(on_set_project, pattern=f"^{PROJECT_SET_PREFIX}"))
    app.add_handler(CallbackQueryHandler(on_project_selected, pattern=r"^proj:"))
    app.add_handler(
        CallbackQueryHandler(on_status_refresh, pattern=f"^{STATUS_REFRESH_DATA}$")
    )
    app.add_handler(CommandHandler("loglevel", cmd_loglevel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(_error_handler)

    if app.job_queue is not None:
        app.job_queue.run_repeating(_send_reminders, interval=300, first=30)
        app.job_queue.run_repeating(_check_orphaned_tasks, interval=300, first=60)
    else:
        logger.warning(
            "Job queue not available (install python-telegram-bot[job-queue]). Reminders and watchdog disabled."
        )

    logger.info("Starting Telegram bot (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
