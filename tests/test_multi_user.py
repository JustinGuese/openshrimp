"""Tests for multi-user support: per-Telegram-user DB isolation."""

import pytest
from sqlmodel import Session

import task_service
from models import User, Project


# ---------------------------------------------------------------------------
# get_or_create_telegram_user
# ---------------------------------------------------------------------------


def test_get_or_create_telegram_user_creates_new(in_memory_engine):
    db_id = task_service.get_or_create_telegram_user(111111, "Alice")
    assert db_id > 0
    with Session(in_memory_engine) as s:
        user = s.get(User, db_id)
    assert user is not None
    assert user.telegram_user_id == 111111
    assert user.name == "Alice"


def test_get_or_create_telegram_user_idempotent(in_memory_engine):
    id1 = task_service.get_or_create_telegram_user(222222, "Bob")
    id2 = task_service.get_or_create_telegram_user(222222, "Bob")
    assert id1 == id2
    with Session(in_memory_engine) as s:
        rows = list(s.exec(
            __import__("sqlmodel").select(User).where(User.telegram_user_id == 222222)
        ))
    assert len(rows) == 1  # no duplicate rows


def test_get_or_create_telegram_user_different_ids_different_users(in_memory_engine):
    id_alice = task_service.get_or_create_telegram_user(333333, "Alice")
    id_bob = task_service.get_or_create_telegram_user(444444, "Bob")
    assert id_alice != id_bob


def test_get_or_create_telegram_user_email_format(in_memory_engine):
    db_id = task_service.get_or_create_telegram_user(555555, "Carol")
    with Session(in_memory_engine) as s:
        user = s.get(User, db_id)
    assert user.email == "tg_555555@openshrimp.local"


# ---------------------------------------------------------------------------
# get_or_create_default_project
# ---------------------------------------------------------------------------


def test_get_or_create_default_project_creates_new(in_memory_engine):
    user_id = task_service.get_or_create_telegram_user(10001, "Dave")
    proj_id = task_service.get_or_create_default_project(user_id)
    assert proj_id > 0
    project = task_service.get_project(proj_id)
    assert project is not None
    assert project.name == "Default"
    assert project.user_id == user_id


def test_get_or_create_default_project_idempotent(in_memory_engine):
    user_id = task_service.get_or_create_telegram_user(10002, "Eve")
    pid1 = task_service.get_or_create_default_project(user_id)
    pid2 = task_service.get_or_create_default_project(user_id)
    assert pid1 == pid2


def test_get_or_create_default_project_per_user_isolation(in_memory_engine):
    uid_a = task_service.get_or_create_telegram_user(10003, "Frank")
    uid_b = task_service.get_or_create_telegram_user(10004, "Grace")
    pid_a = task_service.get_or_create_default_project(uid_a)
    pid_b = task_service.get_or_create_default_project(uid_b)
    assert pid_a != pid_b
    proj_a = task_service.get_project(pid_a)
    proj_b = task_service.get_project(pid_b)
    assert proj_a.user_id == uid_a
    assert proj_b.user_id == uid_b


# ---------------------------------------------------------------------------
# Task list isolation between users
# ---------------------------------------------------------------------------


def test_task_list_isolated_by_user_id(in_memory_engine):
    uid_a = task_service.get_or_create_telegram_user(20001, "Heidi")
    uid_b = task_service.get_or_create_telegram_user(20002, "Ivan")
    pid_a = task_service.get_or_create_default_project(uid_a)
    pid_b = task_service.get_or_create_default_project(uid_b)

    task_service.create_task("Task A1", "desc", uid_a, pid_a)
    task_service.create_task("Task A2", "desc", uid_a, pid_a)
    task_service.create_task("Task B1", "desc", uid_b, pid_b)

    tasks_a = task_service.list_tasks(user_id=uid_a)
    tasks_b = task_service.list_tasks(user_id=uid_b)

    assert len(tasks_a) == 2
    assert len(tasks_b) == 1
    assert all(t.user_id == uid_a for t in tasks_a)
    assert all(t.user_id == uid_b for t in tasks_b)


def test_list_active_summary_isolated_by_user_id(in_memory_engine):
    uid_a = task_service.get_or_create_telegram_user(30001, "Judy")
    uid_b = task_service.get_or_create_telegram_user(30002, "Karl")
    pid_a = task_service.get_or_create_default_project(uid_a)
    pid_b = task_service.get_or_create_default_project(uid_b)

    task_service.create_task("Judy task", "desc", uid_a, pid_a)
    task_service.create_task("Karl task", "desc", uid_b, pid_b)

    summary_a = task_service.list_active_summary(user_id=uid_a)
    summary_b = task_service.list_active_summary(user_id=uid_b)

    assert "Judy task" in summary_a
    assert "Karl task" not in summary_a
    assert "Karl task" in summary_b
    assert "Judy task" not in summary_b


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


def test_get_user_returns_existing(in_memory_engine):
    uid = task_service.get_or_create_telegram_user(40001, "Laura")
    user = task_service.get_user(uid)
    assert user is not None
    assert user.id == uid
    assert user.telegram_user_id == 40001


def test_get_user_returns_none_for_missing(in_memory_engine):
    user = task_service.get_user(99999999)
    assert user is None
