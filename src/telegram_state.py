"""Thread-local holder for the active Telegram session context.

Each worker thread (one per user request) stores its own chat_id, bot app,
event loop, and task_id so that tools can send messages back to the right chat
without passing context through every call frame.
"""

import threading

_local = threading.local()


def set_context(
    chat_id: int,
    bot_app,
    loop,
    task_id: int | None = None,
    human_user_id: int | None = None,
    agent_user_id: int | None = None,
    telegram_user_id: int | None = None,
) -> None:
    """Store Telegram session state for the current thread."""
    _local.chat_id = chat_id
    _local.bot_app = bot_app
    _local.loop = loop
    _local.task_id = task_id
    _local.human_user_id = human_user_id
    _local.agent_user_id = agent_user_id
    _local.telegram_user_id = telegram_user_id


def get_chat_id() -> int | None:
    """Return the chat_id for the current thread, or None if not set."""
    return getattr(_local, "chat_id", None)


def get_bot_app():
    """Return the bot Application for the current thread, or None if not set."""
    return getattr(_local, "bot_app", None)


def get_loop():
    """Return the asyncio event loop for the current thread, or None if not set."""
    return getattr(_local, "loop", None)


def get_task_id() -> int | None:
    """Return the DB task_id for the current thread, or None if not set."""
    return getattr(_local, "task_id", None)


def get_human_user_id() -> int | None:
    """Return the human DB user_id for the current thread, or None if not set."""
    return getattr(_local, "human_user_id", None)


def get_agent_user_id() -> int | None:
    """Return the agent DB user_id for the current thread, or None if not set."""
    return getattr(_local, "agent_user_id", None)


def get_telegram_user_id() -> int | None:
    """Return the Telegram user_id for the current thread, or None if not set."""
    return getattr(_local, "telegram_user_id", None)
