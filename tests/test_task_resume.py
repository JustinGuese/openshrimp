"""Tests for the pending-task auto-pickup behaviour.

Covers:
- chat_id is stored when creating tasks
- _process_pending_tasks picks up pending tasks with chat_id
- _process_pending_tasks skips tasks without chat_id (unless fallback found)
- _process_pending_tasks skips tasks already claimed (worker_id set)
- _lookup_chat_id_for_user falls back to DashboardToken
- _lookup_chat_id_for_user falls back to another task's chat_id
- _process_pending_tasks does not re-pick completed/failed tasks
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session

import db
import task_service
from models import DashboardToken, TaskStatus, User, Project


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seed_users_project(db_session):
    """Create human user, agent user, and project. Return (human_id, agent_id, project_id)."""
    human = User(name="Human", email="human@test.local", passwordhash="")
    db_session.add(human)
    db_session.commit()
    db_session.refresh(human)

    agent = User(name="Agent", email="agent@test.local", passwordhash="")
    db_session.add(agent)
    db_session.commit()
    db_session.refresh(agent)

    project = Project(name="Resume Test", user_id=human.id, description="test")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    return human.id, agent.id, project.id


# ---------------------------------------------------------------------------
# create_task stores chat_id
# ---------------------------------------------------------------------------


def test_create_task_stores_chat_id(seed_users_project):
    human_id, agent_id, project_id = seed_users_project
    task_id = task_service.create_task(
        title="With chat",
        description="D",
        user_id=human_id,
        project_id=project_id,
        assignee_id=agent_id,
        chat_id=12345,
    )
    task = task_service.get_task(task_id)
    assert task.chat_id == 12345


def test_create_task_chat_id_defaults_to_none(seed_users_project):
    human_id, agent_id, project_id = seed_users_project
    task_id = task_service.create_task(
        title="No chat",
        description="D",
        user_id=human_id,
        project_id=project_id,
    )
    task = task_service.get_task(task_id)
    assert task.chat_id is None


# ---------------------------------------------------------------------------
# list_tasks returns tasks with correct filters for pickup
# ---------------------------------------------------------------------------


def test_list_pending_tasks_for_agent(seed_users_project):
    human_id, agent_id, project_id = seed_users_project
    task_service.create_task("T1", "D", human_id, project_id, assignee_id=agent_id, chat_id=111)
    task_service.create_task("T2", "D", human_id, project_id, assignee_id=agent_id, chat_id=222)
    # This one is assigned to human, should NOT appear
    task_service.create_task("T3", "D", human_id, project_id, assignee_id=human_id, chat_id=333)

    pending = task_service.list_tasks(status="pending", assignee_id=agent_id)
    assert len(pending) == 2
    assert all(t.assignee_id == agent_id for t in pending)


def test_completed_tasks_not_in_pending_list(seed_users_project):
    human_id, agent_id, project_id = seed_users_project
    tid = task_service.create_task("Done", "D", human_id, project_id, assignee_id=agent_id, chat_id=111)
    task_service.update_task(tid, status="completed")

    pending = task_service.list_tasks(status="pending", assignee_id=agent_id)
    assert len(pending) == 0


# ---------------------------------------------------------------------------
# _lookup_chat_id_for_user
# ---------------------------------------------------------------------------


def test_lookup_chat_id_from_dashboard_token(seed_users_project, db_session):
    human_id, _, _ = seed_users_project
    token = DashboardToken(token="abc123", user_id=human_id, chat_id=99999)
    db_session.add(token)
    db_session.commit()

    from telegram_bot import _lookup_chat_id_for_user

    result = _lookup_chat_id_for_user(human_id)
    assert result == 99999


def test_lookup_chat_id_falls_back_to_task(seed_users_project):
    human_id, agent_id, project_id = seed_users_project
    # No DashboardToken, but a task with chat_id exists
    task_service.create_task("Old", "D", human_id, project_id, chat_id=77777)

    from telegram_bot import _lookup_chat_id_for_user

    result = _lookup_chat_id_for_user(human_id)
    assert result == 77777


def test_lookup_chat_id_returns_none_when_nothing_found(seed_users_project):
    human_id, _, _ = seed_users_project

    from telegram_bot import _lookup_chat_id_for_user

    result = _lookup_chat_id_for_user(human_id)
    assert result is None


# ---------------------------------------------------------------------------
# _process_pending_tasks integration (sync wrappers around async)
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_globals(seed_users_project, monkeypatch):
    """Patch telegram_bot module globals so _process_pending_tasks can run."""
    human_id, agent_id, project_id = seed_users_project
    import telegram_bot

    monkeypatch.setattr(telegram_bot, "_agent_user_id", agent_id)
    monkeypatch.setattr(telegram_bot, "WORKER_ID", "test-worker-1")
    return human_id, agent_id, project_id


def _make_context():
    """Create a mock context with application.bot.send_message as AsyncMock."""
    ctx = MagicMock()
    ctx.application = MagicMock()
    ctx.application.bot = MagicMock()
    ctx.application.bot.send_message = AsyncMock()
    return ctx


def _run_process_pending(telegram_bot_mod, ctx):
    """Run _process_pending_tasks in a new event loop, mocking out the executor."""
    async def _inner():
        loop = asyncio.get_running_loop()
        with patch.object(loop, "run_in_executor", new_callable=MagicMock):
            await telegram_bot_mod._process_pending_tasks(ctx)

    asyncio.run(_inner())


def test_process_pending_picks_up_task_with_chat_id(bot_globals):
    human_id, agent_id, project_id = bot_globals
    import telegram_bot

    tid = task_service.create_task(
        "Resumable", "Do something", human_id, project_id,
        assignee_id=agent_id, chat_id=55555,
    )

    ctx = _make_context()
    _run_process_pending(telegram_bot, ctx)

    # Task should now have worker_id set (claimed)
    task = task_service.get_task(tid)
    assert task.worker_id == "test-worker-1"

    # Bot should have sent the auto-resume message
    ctx.application.bot.send_message.assert_called()
    call_kwargs = ctx.application.bot.send_message.call_args
    assert call_kwargs.kwargs.get("chat_id") == 55555


def test_process_pending_skips_task_without_chat_id(bot_globals):
    human_id, agent_id, project_id = bot_globals
    import telegram_bot

    # Task with no chat_id and no fallback available
    tid = task_service.create_task(
        "No chat", "D", human_id, project_id,
        assignee_id=agent_id,
    )

    ctx = _make_context()
    _run_process_pending(telegram_bot, ctx)

    # Task should NOT be claimed
    task = task_service.get_task(tid)
    assert task.worker_id is None


def test_process_pending_skips_already_claimed_task(bot_globals):
    human_id, agent_id, project_id = bot_globals
    import telegram_bot

    tid = task_service.create_task(
        "Already claimed", "D", human_id, project_id,
        assignee_id=agent_id, chat_id=55555,
    )
    # Pre-claim with a different worker
    task_service.update_task(tid, worker_id="other-worker")

    ctx = _make_context()
    _run_process_pending(telegram_bot, ctx)

    # Should not have been re-claimed
    task = task_service.get_task(tid)
    assert task.worker_id == "other-worker"

    # No message should have been sent
    ctx.application.bot.send_message.assert_not_called()


def test_process_pending_uses_fallback_chat_id(bot_globals, db_session):
    human_id, agent_id, project_id = bot_globals
    import telegram_bot

    # Task without chat_id
    tid = task_service.create_task(
        "Fallback", "D", human_id, project_id,
        assignee_id=agent_id,
    )

    # But user has a DashboardToken with a chat_id
    token = DashboardToken(token="fallback-token", user_id=human_id, chat_id=88888)
    db_session.add(token)
    db_session.commit()

    ctx = _make_context()
    _run_process_pending(telegram_bot, ctx)

    # Task should be claimed via fallback
    task = task_service.get_task(tid)
    assert task.worker_id == "test-worker-1"

    # Message sent to fallback chat_id
    ctx.application.bot.send_message.assert_called()
    call_kwargs = ctx.application.bot.send_message.call_args
    assert call_kwargs.kwargs.get("chat_id") == 88888


def test_process_pending_does_not_pick_up_completed(bot_globals):
    human_id, agent_id, project_id = bot_globals
    import telegram_bot

    tid = task_service.create_task(
        "Done task", "D", human_id, project_id,
        assignee_id=agent_id, chat_id=55555,
    )
    task_service.update_task(tid, status="completed")

    ctx = _make_context()
    _run_process_pending(telegram_bot, ctx)

    # Nothing should happen — no messages sent
    ctx.application.bot.send_message.assert_not_called()
