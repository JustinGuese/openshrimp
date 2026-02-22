"""Plugin discovery and dynamic loading for openshrimp agent."""

import importlib.util
import json
import logging
from pathlib import Path

from langchain_core.tools import BaseTool

from schemas import PluginManifest

logger = logging.getLogger(__name__)

PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"

# Tool name -> list of plugin tags (e.g. "research"); populated by load_plugins().
TOOL_PLUGIN_TAGS: dict[str, list[str]] = {}


def load_plugins() -> list[BaseTool]:
    """Discover and load all plugins from plugins directory.

    Returns:
        List of loaded BaseTool instances, empty list if no plugins found or all failed.
        Gracefully skips broken plugins and logs warnings.
    """
    tools = []
    TOOL_PLUGIN_TAGS.clear()

    if not PLUGINS_DIR.exists():
        logger.warning(f"Plugins directory does not exist: {PLUGINS_DIR}")
        return tools

    plugin_dirs = sorted([d for d in PLUGINS_DIR.iterdir() if d.is_dir()])
    if not plugin_dirs:
        logger.warning(f"No plugin directories found in {PLUGINS_DIR}")
        return tools

    for plugin_dir in plugin_dirs:
        plugin_name = plugin_dir.name
        manifest_path = plugin_dir / "manifest.json"
        tool_path = plugin_dir / "tool.py"

        # 1. Validate manifest.json
        if not manifest_path.exists():
            logger.warning(f"Plugin '{plugin_name}': manifest.json not found, skipping")
            continue

        try:
            with open(manifest_path) as f:
                manifest_data = json.load(f)
            manifest = PluginManifest(**manifest_data)
        except Exception as e:
            logger.error(f"Plugin '{plugin_name}': failed to validate manifest: {e}, skipping")
            continue

        # 2. Dynamically import tool.py
        if not tool_path.exists():
            logger.error(f"Plugin '{plugin_name}': tool.py not found, skipping")
            continue

        try:
            spec = importlib.util.spec_from_file_location(f"plugin_{plugin_name}_tool", tool_path)
            if spec is None or spec.loader is None:
                logger.error(f"Plugin '{plugin_name}': failed to create spec for tool.py, skipping")
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"Plugin '{plugin_name}': failed to import tool.py: {e}, skipping")
            continue

        # 3. Read module-level TOOLS list
        try:
            module_tools = getattr(module, "TOOLS", [])
            if not isinstance(module_tools, list):
                logger.error(f"Plugin '{plugin_name}': TOOLS is not a list, skipping")
                continue
            if not module_tools:
                logger.warning(f"Plugin '{plugin_name}': TOOLS list is empty")
                continue

            # Validate each item is a BaseTool
            for tool in module_tools:
                if not isinstance(tool, BaseTool):
                    logger.warning(f"Plugin '{plugin_name}': skipping non-BaseTool item: {tool}")
                    continue
                tools.append(tool)
                TOOL_PLUGIN_TAGS[tool.name] = manifest.tags

            tool_names = [t.name for t in module_tools]
            logger.info(f"Loaded plugin '{manifest.name}' v{manifest.version}: {len(module_tools)} tool(s): {tool_names}")
        except Exception as e:
            logger.error(f"Plugin '{plugin_name}': failed to read TOOLS: {e}, skipping")
            continue

    logger.info(f"Plugin loader: {len(tools)} total tool(s) loaded")
    return tools
