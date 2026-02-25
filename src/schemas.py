"""Pydantic models for plugin system.

ToolResult: standardized envelope for plugin return values
PluginManifest: validates plugin metadata from manifest.json
Uses SQLModel (table=False) for consistency with models.py.
"""

from typing import Any, Literal

from sqlmodel import SQLModel


class ToolResult(SQLModel, table=False):
    """Standardized envelope every plugin returns internally."""

    status: Literal["ok", "error"]
    data: str  # always a string for LLM consumption
    plugin: str  # plugin name, e.g. "browser"
    extra: dict[str, Any] = {}  # plugin-specific; avoid name "metadata" (shadows SQLModel.metadata)

    def to_string(self) -> str:
        """Convert to LLM-friendly string output."""
        if self.status == "error":
            return f"[{self.plugin} ERROR] {self.data}"
        return self.data  # plain prose for the LLM; metadata not exposed to LLM


class PluginManifest(SQLModel, table=False):
    """Validates each plugin's manifest.json."""

    name: str
    description: str
    version: str
    tags: list[str] = []
    input_schema: dict[str, Any] = {}
    # Step 2 fields (optional now):
    http_endpoint: str | None = None
    docker_image: str | None = None
