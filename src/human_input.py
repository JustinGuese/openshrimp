"""In-memory registry for pending human-input questions.

When the agent calls ask_human(), a PendingQuestion is registered here.
The Telegram bot checks this registry on every incoming message to decide
whether to route the text to a waiting agent thread instead of spawning a
new research task.

State is lost on process restart. The DB field ``pending_question`` on Task
provides crash-recovery context; ``reset_waiting_tasks()`` in task_service
cleans up orphaned DB state on startup.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class PendingQuestion:
    event: threading.Event
    answer: list[str]        # filled by resolve(); list so it's mutable via reference
    chat_id: int
    question: str
    task_id: int | None
    created_at: float = field(default_factory=time.monotonic)


# keyed by chat_id
_pending: dict[int, PendingQuestion] = {}
_lock = threading.Lock()


def register(chat_id: int, question: str, task_id: int | None) -> PendingQuestion:
    """Register a pending question for *chat_id* and return the PendingQuestion.

    If a previous pending question exists for this chat, it is replaced (the
    old event is NOT signalled — the old caller will time out naturally).
    """
    pq = PendingQuestion(
        event=threading.Event(),
        answer=[],
        chat_id=chat_id,
        question=question,
        task_id=task_id,
    )
    with _lock:
        _pending[chat_id] = pq
    return pq


def resolve(chat_id: int, answer: str) -> bool:
    """Provide the user's answer for a pending question.

    Returns True if there was a pending question, False otherwise.
    """
    with _lock:
        pq = _pending.get(chat_id)
    if pq is None:
        return False
    pq.answer.append(answer)
    pq.event.set()
    return True


def has_pending(chat_id: int) -> bool:
    """Return True if there is a pending question for *chat_id*."""
    with _lock:
        return chat_id in _pending


def get_stale(stale_seconds: float = 300) -> list[PendingQuestion]:
    """Return all pending questions older than *stale_seconds*."""
    cutoff = time.monotonic() - stale_seconds
    with _lock:
        return [pq for pq in _pending.values() if pq.created_at < cutoff]


def cleanup(chat_id: int) -> None:
    """Remove the pending question for *chat_id* (called after it is resolved)."""
    with _lock:
        _pending.pop(chat_id, None)
