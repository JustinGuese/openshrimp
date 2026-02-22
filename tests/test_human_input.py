"""Unit tests for human_input (register, resolve, has_pending, get_stale, cleanup)."""

import time
from unittest.mock import patch

import pytest

import human_input


@pytest.fixture(autouse=True)
def clear_pending():
    """Clear in-memory pending registry before each test for isolation."""
    with human_input._lock:
        human_input._pending.clear()
    yield


def test_register_then_has_pending_returns_true():
    human_input.register(chat_id=1, question="Q?", task_id=42)
    assert human_input.has_pending(1) is True


def test_register_resolve_returns_true_and_sets_answer():
    pq = human_input.register(chat_id=1, question="Q?", task_id=42)
    result = human_input.resolve(1, "Answer text")
    assert result is True
    assert pq.answer == ["Answer text"]
    assert pq.event.is_set()


def test_resolve_unknown_chat_returns_false():
    assert human_input.resolve(999, "x") is False


def test_cleanup_removes_pending_has_pending_false_after():
    human_input.register(chat_id=1, question="Q?", task_id=None)
    human_input.cleanup(1)
    assert human_input.has_pending(1) is False


def test_get_stale_zero_elapsed_returns_empty():
    human_input.register(chat_id=1, question="Q?", task_id=None)
    stale = human_input.get_stale(stale_seconds=300)
    assert stale == []


def test_get_stale_with_mocked_past_returns_entry():
    human_input.register(chat_id=1, question="Q?", task_id=None)
    with human_input._lock:
        pq = human_input._pending[1]
    pq.created_at = 0.0  # make it appear old
    with patch("human_input.time.monotonic", return_value=400.0):
        stale = human_input.get_stale(stale_seconds=300)
    assert len(stale) == 1
    assert stale[0].chat_id == 1


def test_two_sequential_register_same_chat_replaces_first():
    pq1 = human_input.register(chat_id=1, question="First?", task_id=1)
    pq2 = human_input.register(chat_id=1, question="Second?", task_id=2)
    assert pq1 is not pq2
    assert human_input.has_pending(1) is True
    # Resolving should affect the second registration
    human_input.resolve(1, "Answer")
    assert pq2.answer == ["Answer"]
    assert pq1.answer == []
