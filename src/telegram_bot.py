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
import html
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

# Ensure src/ is on sys.path when run directly
_src = Path(__file__).resolve().parent
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from sqlmodel import Session
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

DEFAULT_USER_ID = int(os.environ.get("DEFAULT_USER_ID", "0"))
DEFAULT_PROJECT_ID = int(os.environ.get("DEFAULT_PROJECT_ID", "0"))
DEFAULT_AGENT_USER_ID = int(os.environ.get("DEFAULT_AGENT_USER_ID", "0"))

# Resolved at startup by _ensure_default_user_and_project()
_human_user_id: int = 0
_agent_user_id: int = 0
_effective_project_id: int = 0

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

# Logo path for /start greeting (project root logo.jpg)
_LOGO_PATH = Path(__file__).resolve().parent.parent / "logo.jpg"


@dataclass
class _ProjectPending:
    user_text: str
    auto_name: str  # proposed new project name
    message_id: int  # bot message with the keyboard; used to reject stale taps


_pending_project: dict[int, _ProjectPending] = {}  # keyed by chat_id


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
    """Return existing user or create one. If user_id==0, always create."""
    if user_id:
        user = session.get(User, user_id)
        if user:
            return user
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
    """Set all waiting_for_human tasks to failed (process restarted; events are gone)."""
    count = task_service.reset_waiting_tasks()
    if count:
        logger.warning("Reset %s orphaned waiting_for_human task(s) to failed.", count)


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
        task_service.update_task(task_id, status=TaskStatus.IN_PROGRESS.value, assignee_id=_agent_user_id)
    except Exception as e:
        logger.warning("Failed to mark task %s in_progress: %s", task_id, e)

    def on_progress(tool_name: str, args: dict, observation: str) -> None:
        _on_progress(tool_name, args, observation, app, chat_id, loop)

    failed = False
    body = ""
    try:
        body = run_research(query, on_progress=on_progress) or "(No answer produced)"
        task_service.update_task(task_id, status=TaskStatus.COMPLETED.value)
    except Exception as e:
        logger.exception("Agent failed for task %s: %s", task_id, e)
        body = f"❌ Research failed: {e!r}"
        failed = True
        try:
            task_service.update_task(task_id, status=TaskStatus.FAILED.value)
        except Exception:
            pass

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


async def on_project_selected(update: Update, context) -> None:
    """Handle project selection button tap."""
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

    task_title = user_text[:120]
    try:
        task_id = task_service.create_task(
            title=task_title,
            description=user_text,
            user_id=_human_user_id,
            project_id=project_id,
            assignee_id=_agent_user_id,
        )
    except Exception as e:
        logger.exception("Failed to create task in DB: %s", e)
        await query.edit_message_text(
            BOT_PREFIX + f"⚠️ Could not create task (DB error): {e}"
        )
        return

    active_ctx = task_service.list_active_summary(user_id=_human_user_id)
    research_query = _build_query(user_text, task_id, active_ctx)

    await query.edit_message_text(
        BOT_PREFIX + f"⏳ Starting task #{task_id} in project *{project_name}*…",
        parse_mode="Markdown",
    )

    loop.run_in_executor(
        _executor,
        _run_agent_in_thread,
        research_query,
        chat_id,
        task_id,
        task_title,
        app,
        loop,
    )


async def on_message(update: Update, context) -> None:
    """Handle a free-form text message.

    If there is a pending ask_human question for this chat, route the reply to
    the waiting agent thread. Otherwise, create a new task and run the agent.
    """
    user_text = (update.message.text or "").strip()
    if not user_text:
        return

    chat_id = update.effective_chat.id

    # Route reply to waiting agent thread
    if _human_input.has_pending(chat_id):
        _human_input.resolve(chat_id, user_text)
        return

    # Show project selection keyboard; task creation happens in on_project_selected
    auto_name = _propose_project_name(user_text)
    projects = task_service.list_projects()
    keyboard = _build_project_keyboard(projects, auto_name, _effective_project_id)
    msg = await update.message.reply_text(
        BOT_PREFIX + "📋 Which project should I file this under?",
        reply_markup=keyboard,
    )
    _pending_project[chat_id] = _ProjectPending(
        user_text=user_text, auto_name=auto_name, message_id=msg.message_id
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
    app.add_handler(CallbackQueryHandler(on_project_selected, pattern=r"^proj:"))
    app.add_handler(
        CallbackQueryHandler(on_status_refresh, pattern=f"^{STATUS_REFRESH_DATA}$")
    )
    app.add_handler(CommandHandler("loglevel", cmd_loglevel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if app.job_queue is not None:
        app.job_queue.run_repeating(_send_reminders, interval=300, first=30)
    else:
        logger.warning(
            "Job queue not available (install python-telegram-bot[job-queue]). Reminders disabled."
        )

    logger.info("Starting Telegram bot (polling)…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
