"""Unit tests for task_service (CRUD, reset_waiting_tasks, list_active_summary)."""

import pytest

import task_service
from models import TaskStatus


def test_create_task_returns_valid_id(seed_user_project):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task(
        title="First task",
        description="Desc",
        user_id=user_id,
        project_id=project_id,
    )
    assert isinstance(task_id, int)
    assert task_id >= 1


def test_get_task_returns_task(seed_user_project):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task(
        title="Get me",
        description="D",
        user_id=user_id,
        project_id=project_id,
    )
    task = task_service.get_task(task_id)
    assert task is not None
    assert task.id == task_id
    assert task.title == "Get me"


def test_get_task_returns_none_for_missing_id(seed_user_project):
    assert task_service.get_task(99999) is None


def test_list_tasks_filter_by_user_id(seed_user_project):
    user_id, project_id = seed_user_project
    task_service.create_task("T1", "D", user_id, project_id)
    tasks = task_service.list_tasks(user_id=user_id)
    assert len(tasks) >= 1
    assert all(t.user_id == user_id for t in tasks)


def test_list_tasks_filter_by_status(seed_user_project):
    user_id, project_id = seed_user_project
    task_service.create_task("T1", "D", user_id, project_id)
    task_service.create_task("T2", "D", user_id, project_id)
    tasks = task_service.list_tasks(user_id=user_id, status="pending")
    assert all(t.status == TaskStatus.PENDING for t in tasks)


def test_update_task_with_unset_leaves_assignee_unchanged(seed_user_project):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task("T", "D", user_id, project_id)
    task_service.update_task(task_id, status="in_progress")  # assignee_id = _UNSET
    task = task_service.get_task(task_id)
    assert task.status == TaskStatus.IN_PROGRESS
    assert task.assignee_id is None


def test_update_task_with_explicit_none_clears_assignee(seed_user_project, db_session):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task("T", "D", user_id, project_id)
    task = task_service.get_task(task_id)
    task.assignee_id = user_id
    db_session.add(task)
    db_session.commit()
    task_service.update_task(task_id, assignee_id=None)
    updated = task_service.get_task(task_id)
    assert updated.assignee_id is None


def test_update_task_invalid_status_raises(seed_user_project):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task("T", "D", user_id, project_id)
    with pytest.raises(ValueError, match="Invalid status"):
        task_service.update_task(task_id, status="invalid_status")


def test_reset_waiting_tasks_sets_failed_returns_count(seed_user_project, db_session):
    user_id, project_id = seed_user_project
    task_id = task_service.create_task("T", "D", user_id, project_id)
    task = task_service.get_task(task_id)
    task.status = TaskStatus.WAITING_FOR_HUMAN
    task.pending_question = "Q?"
    db_session.add(task)
    db_session.commit()
    n = task_service.reset_waiting_tasks()
    assert n >= 1
    t = task_service.get_task(task_id)
    assert t.status == TaskStatus.FAILED
    assert t.pending_question is None


def test_list_active_summary_empty_when_no_active_tasks(seed_user_project):
    user_id, project_id = seed_user_project
    # create completed task so there is a project
    task_id = task_service.create_task("Done", "D", user_id, project_id)
    task_service.update_task(task_id, status="completed")
    summary = task_service.list_active_summary()
    assert summary == ""


def test_list_active_summary_includes_project_name_when_tasks_exist(seed_user_project):
    user_id, project_id = seed_user_project
    task_service.create_task("Active one", "D", user_id, project_id)
    summary = task_service.list_active_summary()
    assert "Active one" in summary
    assert "Test Project" in summary
    assert "Active tasks:" in summary
