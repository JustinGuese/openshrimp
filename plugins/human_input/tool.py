"""Human-input plugin for openshrimp.

Provides the ask_human tool, which sends a question to the Telegram user and
blocks the agent thread until the user replies (or the timeout expires).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from langchain_core.tools import tool

# Ensure src/ is on sys.path when loaded via plugin loader
_src = Path(__file__).resolve().parents[2] / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import human_input as _human_input
import task_service
import telegram_state
from models import TaskStatus

PLUGIN_NAME = "human_input"


@tool
def ask_human(question: str, timeout_seconds: int = 600) -> str:
    """Ask the Telegram user a question and wait for their reply.

    Pauses research until the user responds. Returns their answer, or a
    timeout notice if the user does not reply in time.

    Use this when you need clarification, missing context, or a decision
    from the user before you can continue research.

    Args:
        question: The question to ask the user.
        timeout_seconds: How long to wait in seconds (default 600 = 10 min).
    """
    chat_id = telegram_state.get_chat_id()
    bot_app = telegram_state.get_bot_app()
    loop = telegram_state.get_loop()
    task_id = telegram_state.get_task_id()
    human_uid = telegram_state.get_human_user_id()
    agent_uid = telegram_state.get_agent_user_id()

    if not chat_id or not bot_app or not loop:
        return "[human_input] No active Telegram session — cannot ask question."

    # Mark task as waiting and assign to human
    if task_id:
        try:
            task_service.update_task(
                task_id,
                status=TaskStatus.WAITING_FOR_HUMAN.value,
                assignee_id=human_uid,
                pending_question=question,
            )
        except Exception:
            pass  # non-fatal — question still gets asked

    pending = _human_input.register(chat_id, question, task_id)

    try:
        asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(chat_id=chat_id, text=f"❓ {question}"),
            loop,
        ).result(timeout=10)
    except Exception:
        pass  # already sent to user if possible; don't abort

    answered = pending.event.wait(timeout=timeout_seconds)

    # Restore task to agent
    if task_id:
        try:
            task_service.update_task(
                task_id,
                status=TaskStatus.IN_PROGRESS.value,
                assignee_id=agent_uid,
                pending_question=None,
            )
        except Exception:
            pass

    _human_input.cleanup(chat_id)

    if not answered or not pending.answer:
        return "(no answer — user did not reply within the timeout)"
    return pending.answer[0]


TOOLS = [ask_human]
