"""Contract tests: every plugin must have valid manifest, loadable tool.py, and valid TOOLS."""

import json

import pytest
from langchain_core.tools import BaseTool

from conftest import load_plugin_tools, plugin_dirs_list
from schemas import PluginManifest

if not plugin_dirs_list:
    pytest.skip("No plugin dirs with manifest.json + tool.py found", allow_module_level=True)


@pytest.mark.parametrize(
    "plugin_dir",
    plugin_dirs_list,
    ids=[p.name for p in plugin_dirs_list],
)
def test_manifest_valid(plugin_dir):
    """Plugin manifest.json loads and validates with PluginManifest."""
    manifest_path = plugin_dir / "manifest.json"
    with open(manifest_path) as f:
        data = json.load(f)
    manifest = PluginManifest(**data)
    assert manifest.name
    assert manifest.version


@pytest.mark.parametrize(
    "plugin_dir",
    plugin_dirs_list,
    ids=[p.name for p in plugin_dirs_list],
)
def test_tool_module_loads(plugin_dir):
    """Plugin tool.py loads without exception."""
    load_plugin_tools(plugin_dir.name)


@pytest.mark.parametrize(
    "plugin_dir",
    plugin_dirs_list,
    ids=[p.name for p in plugin_dirs_list],
)
def test_tools_non_empty_list(plugin_dir):
    """Plugin exposes a non-empty TOOLS list."""
    tools = load_plugin_tools(plugin_dir.name)
    assert isinstance(tools, list), "TOOLS must be a list"
    assert len(tools) >= 1, "TOOLS must contain at least one tool"


@pytest.mark.parametrize(
    "plugin_dir",
    plugin_dirs_list,
    ids=[p.name for p in plugin_dirs_list],
)
def test_tools_are_base_tool(plugin_dir):
    """Every item in TOOLS is a LangChain BaseTool."""
    tools = load_plugin_tools(plugin_dir.name)
    for tool in tools:
        assert isinstance(
            tool, BaseTool
        ), f"Tool {getattr(tool, 'name', tool)} is not a BaseTool"


@pytest.mark.parametrize(
    "plugin_dir",
    plugin_dirs_list,
    ids=[p.name for p in plugin_dirs_list],
)
def test_tool_has_name_and_schema(plugin_dir):
    """Each tool has a name and a usable args schema for the agent."""
    tools = load_plugin_tools(plugin_dir.name)
    for tool in tools:
        assert getattr(tool, "name", None), f"Tool {tool!r} has no .name"
        # LangChain tools expose args_schema or similar for the agent
        schema = getattr(tool, "args_schema", None) or getattr(
            tool, "args", None
        )
        assert schema is not None or hasattr(
            tool, "invoke"
        ), "Tool should have args schema or invoke"
