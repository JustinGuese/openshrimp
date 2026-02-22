"""Telegram notify tool for openshrimp plugin system.

Allows the agent to send a proactive update to the Telegram user mid-research.
This is a no-op when not running inside a Telegram bot context.
"""

import asyncio
import sys
from pathlib import Path

from langchain_core.tools import tool

# Add src directory to path so we can import telegram_state when run via plugin loader
_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import telegram_state

PLUGIN_NAME = "telegram_notify"


@tool
def telegram_send(message: str) -> str:
    """Send a proactive update message to the Telegram user while still working.

    Use this to share intermediate findings or let the user know what you are doing.
    This is a no-op if not running inside a Telegram bot context.

    Args:
        message: The message to send to the user.
    """
    chat_id = telegram_state.get_chat_id()
    bot_app = telegram_state.get_bot_app()
    loop = telegram_state.get_loop()

    if chat_id is None or bot_app is None or loop is None:
        return "[telegram_notify] No active Telegram session — message not sent."

    try:
        future = asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(chat_id=chat_id, text=message),
            loop,
        )
        future.result(timeout=10)
        return f"[telegram_notify] Message sent to chat {chat_id}."
    except Exception as e:
        return f"[telegram_notify] Failed to send message: {e!r}"


TOOLS = [telegram_send]
