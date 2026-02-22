"""Verify Task/Project datetime default_factory: each instance gets a fresh timestamp."""

import time

from models import Task


def test_task_created_at_uses_default_factory():
    """Two Task instances created with a small delay have different created_at."""
    t1 = Task(title="a", user_id=1, project_id=1, description="x")
    time.sleep(0.02)
    t2 = Task(title="b", user_id=1, project_id=1, description="y")
    assert t1.created_at != t2.created_at
    assert t1.created_at <= t2.created_at
