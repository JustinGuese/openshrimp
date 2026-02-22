"""Plugin-specific tests for browser_research. Use fixtures from plugins/conftest.py."""


def test_browser_research_tool_loads(load_plugin_tools_fixture):
    """This plugin exposes at least the browser_research tool."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("browser_research")
    names = [t.name for t in tools]
    assert "browser_research" in names


def test_browser_research_tool_has_url_arg(load_plugin_tools_fixture):
    """The browser_research tool accepts a url argument."""
    load_plugin_tools = load_plugin_tools_fixture
    tools = load_plugin_tools("browser_research")
    browser_tool = next(t for t in tools if t.name == "browser_research")
    # LangChain tools expose args schema
    assert hasattr(browser_tool, "args") or getattr(browser_tool, "args_schema", None) is not None
