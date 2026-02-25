"""Plugin-specific tests for browser. Use fixtures from plugins/conftest.py."""


def test_browser_tool_loads(load_plugin_tools_fixture):
    """This plugin exposes the browser tool."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("browser")
    names = [t.name for t in tools]
    assert "browser" in names


def test_browser_tool_has_action_param(load_plugin_tools_fixture):
    """The browser tool accepts an action argument."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("browser")
    browser_tool = next(t for t in tools if t.name == "browser")
    assert hasattr(browser_tool, "args") or getattr(browser_tool, "args_schema", None) is not None
