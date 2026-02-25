"""Credential vault plugin for openshrimp.

Thin @tool wrappers around the encrypted credential store in credentials.py.
Secrets are scoped to the current task's project and user and encrypted with
OPENSHRIMP_VAULT_KEY.
"""

from __future__ import annotations

import sys
from pathlib import Path

from langchain_core.tools import tool

# Ensure src/ is on sys.path when loaded via plugin loader
_src = Path(__file__).resolve().parents[2]
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

import credentials as cred_vault
import task_service
import telegram_state
from schemas import ToolResult

PLUGIN_NAME = "credential_vault"


def _ok(data: str, metadata: dict | None = None) -> str:
    return ToolResult(status="ok", data=data, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


def _err(msg: str, metadata: dict | None = None) -> str:
    return ToolResult(status="error", data=msg, plugin=PLUGIN_NAME, extra=metadata or {}).to_string()


def _current_project_and_user() -> tuple[int | None, int | None]:
    """Best-effort resolution of (project_id, user_id) from the active task context."""
    task_id = telegram_state.get_task_id()
    if task_id:
        task = task_service.get_task(task_id)
        if task is not None:
            return task.project_id, task.user_id
    # Fallback: no task context; project unknown
    human_user_id = telegram_state.get_human_user_id()
    return None, human_user_id


@tool
def store_credential(name: str, secret: str) -> str:
    """Store a secret (password, API token, etc.) in the credential vault.

    Secrets are encrypted with OPENSHRIMP_VAULT_KEY and scoped to the current
    task's project and user. Use concise, deterministic names such as
    "twitter/main" or "github/personal".
    """
    project_id, user_id = _current_project_and_user()
    if project_id is None:
        return _err(
            "No active task/project context found; cannot determine where to store the credential."
        )
    try:
        cred_vault.store_secret(project_id=project_id, user_id=user_id, name=name, value=secret)
        return _ok(f"Stored credential '{name}' for project_id={project_id}.")
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:  # pragma: no cover - defensive
        return _err(f"Failed to store credential: {e!r}")


@tool
def get_credential(name: str) -> str:
    """Retrieve a previously stored secret from the credential vault.

    Looks up the secret for the current task's project and user.
    Returns a human-readable message; the secret value is also included in
    metadata for machine consumption.
    """
    project_id, user_id = _current_project_and_user()
    if project_id is None:
        return _err(
            "No active task/project context found; cannot determine which project vault to use."
        )
    try:
        value = cred_vault.get_secret(project_id=project_id, user_id=user_id, name=name)
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:  # pragma: no cover - defensive
        return _err(f"Failed to retrieve credential: {e!r}")
    if value is None:
        return _err(f"No credential named '{name}' found for this project.")
    return _ok(
        f"Credential '{name}' retrieved successfully.",
        metadata={"name": name, "secret": value},
    )


@tool
def list_credentials() -> str:
    """List credential names available for the current task's project and user.

    Does NOT return secret values, only names.
    """
    project_id, user_id = _current_project_and_user()
    if project_id is None:
        return _err("No active task/project context found; cannot list credentials.")
    try:
        names = cred_vault.list_secret_names(project_id=project_id, user_id=user_id)
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:  # pragma: no cover - defensive
        return _err(f"Failed to list credentials: {e!r}")
    if not names:
        return _ok("No credentials stored for this project.", metadata={"credentials": []})
    lines = "\n".join(f"- {n}" for n in names)
    return _ok(
        f"Stored credentials for this project:\n{lines}",
        metadata={"credentials": names},
    )


@tool
def delete_credential(name: str) -> str:
    """Delete a stored credential for the current task's project and user."""
    project_id, user_id = _current_project_and_user()
    if project_id is None:
        return _err("No active task/project context found; cannot delete credentials.")
    try:
        deleted = cred_vault.delete_secret(project_id=project_id, user_id=user_id, name=name)
    except RuntimeError as e:
        return _err(str(e))
    except Exception as e:  # pragma: no cover - defensive
        return _err(f"Failed to delete credential: {e!r}")
    if not deleted:
        return _ok(
            f"No credential named '{name}' existed for this project.",
            metadata={"deleted": False},
        )
    return _ok(
        f"Deleted credential '{name}' for this project.",
        metadata={"deleted": True},
    )


TOOLS = [store_credential, get_credential, list_credentials, delete_credential]

