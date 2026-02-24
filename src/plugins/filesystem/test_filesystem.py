"""Tests for the filesystem plugin: project-scoped file and folder operations."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from conftest import load_plugin_tools

# Ensure src is importable for telegram_state
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

TEST_PROJECT_ID = 999999


@pytest.fixture
def filesystem_tools():
    return load_plugin_tools("filesystem")


@pytest.fixture(autouse=True)
def cleanup_test_workspace():
    yield
    import shutil
    root = Path(__file__).resolve().parent / "workspaces"
    # New path: workspaces/shared/<project_id>/ (no Telegram context in tests)
    test_dir = root / "shared" / str(TEST_PROJECT_ID)
    if test_dir.exists():
        shutil.rmtree(test_dir)
    # Also clean old-style path if it exists
    old_test_dir = root / str(TEST_PROJECT_ID)
    if old_test_dir.exists():
        shutil.rmtree(old_test_dir)


def test_filesystem_tools_load(filesystem_tools):
    names = [t.name for t in filesystem_tools]
    assert "read_file" in names
    assert "write_file" in names
    assert "search_replace_file" in names
    assert "create_folder" in names
    assert "delete_file" in names
    assert "delete_folder" in names
    assert "list_directory" in names
    assert "run_command" in names


def test_write_and_read_file(filesystem_tools):
    write = next(t for t in filesystem_tools if t.name == "write_file")
    read = next(t for t in filesystem_tools if t.name == "read_file")
    out = write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "hello.txt",
        "content": "Hello world",
    })
    assert "ERROR" not in out
    out2 = read.invoke({"project_id": TEST_PROJECT_ID, "path": "hello.txt"})
    assert "ERROR" not in out2
    assert "Hello world" in out2


def test_search_replace_file(filesystem_tools):
    write = next(t for t in filesystem_tools if t.name == "write_file")
    search_replace = next(t for t in filesystem_tools if t.name == "search_replace_file")
    read = next(t for t in filesystem_tools if t.name == "read_file")
    write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "src/main.py",
        "content": "def foo():\n    return 42\n",
    })
    out = search_replace.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "src/main.py",
        "old_string": "return 42",
        "new_string": "return 43",
    })
    assert "ERROR" not in out
    out2 = read.invoke({"project_id": TEST_PROJECT_ID, "path": "src/main.py"})
    assert "return 43" in out2
    assert "return 42" not in out2


def test_search_replace_old_string_not_found(filesystem_tools):
    write = next(t for t in filesystem_tools if t.name == "write_file")
    search_replace = next(t for t in filesystem_tools if t.name == "search_replace_file")
    write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "x.txt",
        "content": "only this",
    })
    out = search_replace.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "x.txt",
        "old_string": "nonexistent",
        "new_string": "y",
    })
    assert "ERROR" in out
    assert "write_file" in out or "replace" in out.lower()


def test_create_folder_and_list_directory(filesystem_tools):
    create_folder = next(t for t in filesystem_tools if t.name == "create_folder")
    list_dir = next(t for t in filesystem_tools if t.name == "list_directory")
    create_folder.invoke({"project_id": TEST_PROJECT_ID, "path": "docs"})
    create_folder.invoke({"project_id": TEST_PROJECT_ID, "path": "src/utils"})
    out = list_dir.invoke({"project_id": TEST_PROJECT_ID, "path": "."})
    assert "ERROR" not in out
    assert "docs" in out
    assert "src" in out or "utils" in out


def test_delete_file(filesystem_tools):
    write = next(t for t in filesystem_tools if t.name == "write_file")
    delete_file = next(t for t in filesystem_tools if t.name == "delete_file")
    read = next(t for t in filesystem_tools if t.name == "read_file")
    write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "to_delete.txt",
        "content": "x",
    })
    out = delete_file.invoke({"project_id": TEST_PROJECT_ID, "path": "to_delete.txt"})
    assert "ERROR" not in out
    out2 = read.invoke({"project_id": TEST_PROJECT_ID, "path": "to_delete.txt"})
    assert "ERROR" in out2


def test_delete_folder(filesystem_tools):
    create_folder = next(t for t in filesystem_tools if t.name == "create_folder")
    write = next(t for t in filesystem_tools if t.name == "write_file")
    delete_folder = next(t for t in filesystem_tools if t.name == "delete_folder")
    list_dir = next(t for t in filesystem_tools if t.name == "list_directory")
    create_folder.invoke({"project_id": TEST_PROJECT_ID, "path": "to_remove"})
    write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "to_remove/nested.txt",
        "content": "nested",
    })
    out = delete_folder.invoke({"project_id": TEST_PROJECT_ID, "path": "to_remove"})
    assert "ERROR" not in out
    out2 = list_dir.invoke({"project_id": TEST_PROJECT_ID, "path": "."})
    assert "to_remove" not in out2


def test_path_traversal_rejected(filesystem_tools):
    write = next(t for t in filesystem_tools if t.name == "write_file")
    out = write.invoke({
        "project_id": TEST_PROJECT_ID,
        "path": "../../../etc/passwd",
        "content": "x",
    })
    assert "ERROR" in out
    assert "escape" in out.lower() or "Path" in out


def test_run_command_cwd_is_project_workspace(filesystem_tools):
    """run_command runs in the project workspace and returns stdout + exit code."""
    run_cmd = next(t for t in filesystem_tools if t.name == "run_command")
    out = run_cmd.invoke({
        "project_id": TEST_PROJECT_ID,
        "command": "echo ran_in_workspace",
        "timeout_seconds": 5,
    })
    assert "ERROR" not in out
    assert "ran_in_workspace" in out
    assert "exit code: 0" in out


# ---------------------------------------------------------------------------
# Workspace path isolation tests
# ---------------------------------------------------------------------------


def _get_project_dir(project_id):
    """Import _project_dir directly from the filesystem tool module."""
    import importlib.util
    tool_path = Path(__file__).resolve().parent / "tool.py"
    spec = importlib.util.spec_from_file_location("fs_tool_mod", tool_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._project_dir(project_id)


def test_project_dir_uses_shared_when_no_context():
    """Without a Telegram context, the workspace is under 'shared/'."""
    import telegram_state
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=None)
    path = _get_project_dir(TEST_PROJECT_ID)
    assert path.parts[-2] == "shared"


def test_project_dir_uses_telegram_user_id_from_context():
    """When a telegram_user_id is set in thread-local state, it appears in the path."""
    import telegram_state
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=12345)
    path = _get_project_dir(TEST_PROJECT_ID)
    assert "12345" in path.parts


def test_project_dir_isolates_two_users():
    """Two different telegram_user_ids produce different workspace paths."""
    import telegram_state
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=11111)
    path_a = _get_project_dir(TEST_PROJECT_ID)
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=22222)
    path_b = _get_project_dir(TEST_PROJECT_ID)
    assert path_a != path_b
    assert "11111" in str(path_a)
    assert "22222" in str(path_b)


def test_project_dir_uses_project_name_when_available():
    """When task_service returns a project, the directory uses its sanitized name."""
    import telegram_state
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=99999)

    fake_project = MagicMock()
    fake_project.name = "My Cool Project"

    with patch("task_service.get_project", return_value=fake_project):
        path = _get_project_dir(TEST_PROJECT_ID)

    assert "My_Cool_Project" in path.parts


def test_project_dir_falls_back_to_id_when_project_missing():
    """When task_service returns None (project not found), falls back to project_id."""
    import telegram_state
    telegram_state.set_context(chat_id=0, bot_app=None, loop=None, telegram_user_id=99999)

    with patch("task_service.get_project", return_value=None):
        path = _get_project_dir(TEST_PROJECT_ID)

    assert str(TEST_PROJECT_ID) in path.parts
