"""Filesystem tool for openshrimp plugin system.

Project-scoped file and folder operations. Workspace root: src/plugins/filesystem/workspaces/<project_id>/.
All paths are relative to that directory. Use search_replace_file for targeted edits; if old_string
is not found, use write_file to replace the whole file. run_command runs terminal commands with
the project workspace as the current working directory.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

# Add src directory to path so we can import schemas when run standalone or via plugin loader
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from schemas import ToolResult

PLUGIN_NAME = "filesystem"


def _workspace_root() -> Path:
    return Path(__file__).resolve().parent / "workspaces"


def _project_dir(project_id: int) -> Path:
    return _workspace_root() / str(project_id)


def _resolve(project_id: int, path: str, must_exist: bool = False) -> Path:
    """Resolve path relative to project workspace; raise if it escapes (path traversal)."""
    base = _project_dir(project_id)
    # Normalize: no leading slash, collapse ..
    p = path.strip().replace("\\", "/").lstrip("/")
    if not p:
        p = "."
    resolved = (base / p).resolve()
    if not resolved.is_relative_to(base):
        raise ValueError("Path escapes project workspace")
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"Path does not exist: {path}")
    return resolved


def _ok(data: str, metadata: dict | None = None) -> str:
    return ToolResult(status="ok", data=data, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


def _err(msg: str, metadata: dict | None = None) -> str:
    return ToolResult(status="error", data=msg, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


@tool
def read_file(project_id: int, path: str) -> str:
    """Read the contents of a file in the project workspace.

    Use the task's project_id so files are read from that project's workspace.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the file (e.g. src/main.py).
    """
    try:
        fp = _resolve(project_id, path, must_exist=True)
        if not fp.is_file():
            return _err(f"Not a file: {path}")
        return _ok(fp.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))


@tool
def write_file(project_id: int, path: str, content: str) -> str:
    """Create or overwrite a file in the project workspace.

    Use for new files or when replacing the entire file content (e.g. when search_replace_file
    fails because the old_string was not found).

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the file (e.g. src/main.py).
        content: Full file content to write.
    """
    try:
        fp = _resolve(project_id, path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        _project_dir(project_id).mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        return _ok(f"Wrote {path}")
    except ValueError as e:
        return _err(str(e))


@tool
def search_replace_file(
    project_id: int,
    path: str,
    old_string: str,
    new_string: str,
) -> str:
    """Replace the first occurrence of old_string with new_string in a file.

    Prefer this for targeted edits. If old_string is not found in the file, the tool returns
    an error suggesting you use write_file to replace the whole file.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the file.
        old_string: Exact string to find and replace (first occurrence only).
        new_string: String to insert in place of old_string.
    """
    try:
        fp = _resolve(project_id, path, must_exist=True)
        if not fp.is_file():
            return _err(f"Not a file: {path}")
        text = fp.read_text(encoding="utf-8", errors="replace")
        if old_string not in text:
            return _err(
                f"old_string not found in {path}. Use write_file to replace the entire file content."
            )
        new_text = text.replace(old_string, new_string, 1)
        fp.write_text(new_text, encoding="utf-8")
        return _ok(f"Replaced first occurrence in {path}")
    except FileNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))


@tool
def create_folder(project_id: int, path: str) -> str:
    """Create a directory (and parent directories) in the project workspace.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path for the new directory (e.g. src/utils or docs).
    """
    try:
        fp = _resolve(project_id, path)
        _project_dir(project_id).mkdir(parents=True, exist_ok=True)
        fp.mkdir(parents=True, exist_ok=True)
        return _ok(f"Created directory {path}")
    except ValueError as e:
        return _err(str(e))


@tool
def delete_file(project_id: int, path: str) -> str:
    """Delete a file in the project workspace.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the file to delete.
    """
    try:
        fp = _resolve(project_id, path, must_exist=True)
        if not fp.is_file():
            return _err(f"Not a file: {path}")
        fp.unlink()
        return _ok(f"Deleted {path}")
    except FileNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))


@tool
def delete_folder(project_id: int, path: str) -> str:
    """Delete a directory and all its contents in the project workspace.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the directory to delete.
    """
    try:
        fp = _resolve(project_id, path, must_exist=True)
        if not fp.is_dir():
            return _err(f"Not a directory: {path}")
        shutil.rmtree(fp)
        return _ok(f"Deleted directory {path}")
    except FileNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))


@tool
def list_directory(project_id: int, path: str = ".") -> str:
    """List entries (files and folders) in a directory in the project workspace.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder.
        path: Relative path to the directory (default: root of project workspace).
    """
    try:
        fp = _resolve(project_id, path, must_exist=True)
        if not fp.is_dir():
            return _err(f"Not a directory: {path}")
        entries = sorted(fp.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = [f"{'[dir]  ' if e.is_dir() else '       '} {e.name}" for e in entries]
        return _ok("\n".join(lines) if lines else "(empty)")
    except FileNotFoundError as e:
        return _err(str(e))
    except ValueError as e:
        return _err(str(e))


@tool
def run_command(
    project_id: int,
    command: str,
    timeout_seconds: int = 120,
) -> str:
    """Run a terminal command with the project workspace as the current working directory.

    Use for running scripts, package managers, or any shell command (e.g. npm install,
    python main.py, ls -la). The command runs inside the project's workspace folder.
    Stdout and stderr are captured; exit code is reported.

    Args:
        project_id: Project ID (from task tracking); determines the workspace folder (cwd).
        command: The shell command to run (e.g. "npm install" or "python -m pytest").
        timeout_seconds: Maximum time the command may run (default 120). After this, it is killed.
    """
    try:
        cwd = _project_dir(project_id)
        cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            errors="replace",
        )
        out_lines = []
        if result.stdout:
            out_lines.append(result.stdout.rstrip())
        if result.stderr:
            out_lines.append(result.stderr.rstrip())
        body = "\n".join(out_lines) if out_lines else "(no output)"
        body += f"\n[exit code: {result.returncode}]"
        return _ok(body)
    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {timeout_seconds} seconds.")
    except ValueError as e:
        return _err(str(e))


TOOLS = [
    read_file,
    write_file,
    search_replace_file,
    create_folder,
    delete_file,
    delete_folder,
    list_directory,
    run_command,
]
