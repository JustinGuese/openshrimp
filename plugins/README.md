# Plugin tests

Run all plugin tests from the repo root:

```bash
uv run pytest plugins/
# or
uv run pytest
```

## Layout

- **Contract tests** (`plugins/test_plugin_contract.py`): Run for every plugin that has `manifest.json` and `tool.py`. They check manifest validity, loadable `tool.py`, and a valid non-empty `TOOLS` list of LangChain `BaseTool` instances. No per-plugin code required.

- **Per-plugin tests**: Add a test file with a unique name under your plugin, e.g. `plugins/<plugin_name>/test_<plugin_name>.py` (e.g. `test_browser_research.py`). Using a unique name avoids pytest collection clashes with other plugins. Use the shared fixtures from `plugins/conftest.py`:
  - **`plugin_dirs`**: list of Paths to each plugin directory (with manifest + tool.py).
  - **`load_plugin_tools_fixture`**: fixture that returns a callable `load_plugin_tools(plugin_name)` to load and cache that plugin’s `TOOLS` list.

Example:

```python
from conftest import load_plugin_tools

def test_my_tool_loads(load_plugin_tools_fixture):
    tools = load_plugin_tools_fixture("my_plugin")
    assert any(t.name == "my_tool" for t in tools)
```

## Adding a new plugin

1. Create `plugins/<name>/manifest.json` and `plugins/<name>/tool.py` with a `TOOLS` list.
2. Contract tests will run for it automatically.
3. Optionally add `plugins/<name>/test_<name>.py` for plugin-specific tests (e.g. invoking tools with mocks). Use a unique filename so pytest does not confuse modules across plugins.
