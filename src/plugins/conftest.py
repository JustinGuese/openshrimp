"""Shared pytest fixtures for plugin tests.

Ensures src and repo root are on sys.path. Provides plugin discovery and
a callable to load a plugin's TOOLS list (cached per plugin name).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGINS_DIR = Path(__file__).resolve().parent
SRC_DIR = PLUGINS_DIR.parent
REPO_ROOT = SRC_DIR.parent

for path in (SRC_DIR, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _discover_plugin_dirs():
    """Plugin dirs that have both manifest.json and tool.py."""
    if not PLUGINS_DIR.exists():
        return []
    return sorted(
        d
        for d in PLUGINS_DIR.iterdir()
        if d.is_dir() and (d / "manifest.json").exists() and (d / "tool.py").exists()
    )


plugin_dirs_list = _discover_plugin_dirs()

_plugin_tools_cache = {}


def load_plugin_tools(plugin_name: str):
    """Load and return the TOOLS list for a plugin. Cached per plugin name."""
    if plugin_name in _plugin_tools_cache:
        return _plugin_tools_cache[plugin_name]
    plugin_dir = PLUGINS_DIR / plugin_name
    tool_path = plugin_dir / "tool.py"
    if not tool_path.exists():
        raise FileNotFoundError(f"Plugin '{plugin_name}': tool.py not found")
    spec = importlib.util.spec_from_file_location(
        f"plugin_{plugin_name}_tool", tool_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Plugin '{plugin_name}': failed to create spec for tool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = getattr(module, "TOOLS", [])
    _plugin_tools_cache[plugin_name] = tools
    return tools


@pytest.fixture(scope="session")
def plugin_dirs():
    """List of Paths to plugin directories that have manifest.json and tool.py."""
    return list(plugin_dirs_list)


@pytest.fixture(scope="session")
def load_plugin_tools_fixture():
    """Return the load_plugin_tools(plugin_name) callable. Cached per plugin."""
    return load_plugin_tools
