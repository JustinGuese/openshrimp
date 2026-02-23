"""Human-input plugin for openshrimp.

Provides the ask_human tool, which sends a question to the Telegram user and
blocks the agent thread until the user replies (or the timeout expires).

Guards:
  - LLM evaluator rejects trivial/unnecessary questions before they reach the user.
  - Per-task question counter hard-caps at MAX_ASKS_PER_TASK (env var, default 2).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from langchain_core.tools import tool

# Ensure src/ is on sys.path when loaded via plugin loader
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import human_input as _human_input
import task_service
import telegram_state
from models import TaskStatus

PLUGIN_NAME = "human_input"
logger = logging.getLogger(PLUGIN_NAME)

MAX_ASKS_PER_TASK: int = int(os.environ.get("MAX_ASKS_PER_TASK", "2"))

# Per-task question counter: task_id → count of questions actually sent to user
_ask_count: dict[int, int] = {}
_ask_count_lock = threading.Lock()


def _increment_ask_count(task_id: int) -> int:
    """Increment and return the new question count for this task."""
    with _ask_count_lock:
        _ask_count[task_id] = _ask_count.get(task_id, 0) + 1
        return _ask_count[task_id]


def _get_ask_count(task_id: int) -> int:
    with _ask_count_lock:
        return _ask_count.get(task_id, 0)


_MAX_ASK_COUNT_ENTRIES = 500


def _cleanup_ask_count() -> None:
    """Evict old entries if dict grows too large."""
    with _ask_count_lock:
        if len(_ask_count) > _MAX_ASK_COUNT_ENTRIES:
            sorted_ids = sorted(_ask_count.keys())
            for tid in sorted_ids[:len(sorted_ids) // 2]:
                del _ask_count[tid]


def _should_ask_user(question: str) -> tuple[bool, str]:
    """Use a fast LLM call to decide whether the question is worth bothering the user.

    Returns (should_allow: bool, suggestion: str).
    - should_allow=True  → let the question through
    - should_allow=False → suggestion contains the assumption the agent should use instead
    On any error, fails open (returns True, "").
    """
    try:
        import os as _os
        api_key = _os.environ.get("OPENROUTER_API_KEY") or _os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return True, ""

        from langchain_openrouter import ChatOpenRouter
        from langchain_core.messages import HumanMessage

        llm = ChatOpenRouter(
            model=_os.environ.get("OPENROUTER_EVALUATOR_MODEL", _os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")),
            temperature=0,
            api_key=api_key,
            timeout=15_000,  # 15 s — fast gate check
        )
        eval_prompt = (
            "You are a gatekeeper deciding whether an AI research agent should ask a human user a question.\n\n"
            "Approve (ALLOW) only if:\n"
            "- The question cannot be answered by web search or existing knowledge.\n"
            "- It involves personal preferences, credentials, or genuinely ambiguous requirements.\n\n"
            "Reject (REJECT) if:\n"
            "- The agent could make a reasonable assumption and proceed.\n"
            "- The question is 'should I continue?', 'do you want more detail?', or similar filler.\n"
            "- The question is factual and could be answered via search.\n\n"
            f"Question: {question}\n\n"
            "Reply with EXACTLY one of:\n"
            "ALLOW\n"
            "REJECT: <brief suggested assumption the agent should use instead>"
        )
        response = llm.invoke([HumanMessage(content=eval_prompt)])
        text = (getattr(response, "content", "") or "").strip()
        if text.upper().startswith("ALLOW"):
            return True, ""
        if text.upper().startswith("REJECT"):
            suggestion = text[len("REJECT"):].lstrip(": ").strip()
            return False, suggestion or "Proceed with your best judgment."
        # Unexpected format — fail open
        logger.debug("ask_human evaluator unexpected response: %r", text)
        return True, ""
    except Exception as exc:
        logger.debug("ask_human evaluator error (failing open): %s", exc)
        return True, ""


@tool
def ask_human(question: str, timeout_seconds: int = 600) -> str:
    """Ask the Telegram user a question and wait for their reply.

    Pauses research until the user responds. Returns their answer, or a
    timeout notice if the user does not reply in time.

    Use this ONLY when you genuinely cannot proceed without user input:
    - Ambiguous personal preferences
    - Missing credentials or access tokens
    - Genuinely unclear requirements that cannot be resolved by searching

    Do NOT use this for factual questions, confirmations, or "should I continue?" checks.

    Args:
        question: The question to ask the user.
        timeout_seconds: How long to wait in seconds (default 600 = 10 min).
    """
    _cleanup_ask_count()

    chat_id = telegram_state.get_chat_id()
    bot_app = telegram_state.get_bot_app()
    loop = telegram_state.get_loop()
    task_id = telegram_state.get_task_id()
    human_uid = telegram_state.get_human_user_id()
    agent_uid = telegram_state.get_agent_user_id()

    if not chat_id or not bot_app or not loop:
        return "[human_input] No active Telegram session — cannot ask question."

    # Hard backstop: per-task question limit
    if task_id is not None and _get_ask_count(task_id) >= MAX_ASKS_PER_TASK:
        logger.info(
            "ask_human blocked for task #%s: already asked %d/%d questions.",
            task_id, _get_ask_count(task_id), MAX_ASKS_PER_TASK,
        )
        return (
            f"(Question not sent — maximum of {MAX_ASKS_PER_TASK} questions per task reached. "
            "Proceed with your best judgment based on available information.)"
        )

    # LLM evaluator gate: reject trivial or unnecessary questions
    allowed, suggestion = _should_ask_user(question)
    if not allowed:
        logger.info("ask_human evaluator rejected question for task #%s: %r", task_id, question)
        return (
            f"(Question not sent to user — make this assumption instead: {suggestion})"
        )

    # Increment counter for this task
    if task_id is not None:
        _increment_ask_count(task_id)

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
