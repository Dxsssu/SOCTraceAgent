from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    server_name: str
    name: str
    description: str
    tool: Any
    enabled_for_routing: bool = True
    when_to_use: str = ""
    limitations: str = ""

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = _run_async(self.tool.run(kwargs))
        if hasattr(result, "structured_content") and isinstance(result.structured_content, dict):
            return result.structured_content

        content = getattr(result, "content", None) or []
        if content:
            text = getattr(content[0], "text", "")
            if text:
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict):
                    return parsed
        return {
            "success": False,
            "result": {},
            "error_message": "tool_execution_error:FastMCP tool returned an unsupported response format",
            "tool_input": {},
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "name": self.name,
            "description": self.description,
            "enabled_for_routing": self.enabled_for_routing,
            "when_to_use": self.when_to_use,
            "limitations": self.limitations,
        }


_SERVER_REGISTRY: dict[str, FastMCP] = {}


def register_server(server: FastMCP) -> FastMCP:
    _SERVER_REGISTRY[server.name] = server
    return server


def get_server(name: str) -> FastMCP:
    return _SERVER_REGISTRY[name]


def get_registered_tool(name: str) -> ToolDefinition | None:
    for tool in list_registered_tools(include_non_routable=True):
        if tool.name == name:
            return tool
    return None


def list_registered_tools(
    *,
    include_non_routable: bool = True,
    exclude_names: set[str] | None = None,
) -> list[ToolDefinition]:
    excluded = exclude_names or set()
    registered_tools: list[ToolDefinition] = []
    for server_name, server in _SERVER_REGISTRY.items():
        fastmcp_tools = _run_async(server.list_tools())
        for tool in fastmcp_tools:
            metadata = dict(getattr(tool, "meta", None) or {})
            definition = ToolDefinition(
                server_name=server_name,
                name=str(getattr(tool, "name", "") or ""),
                description=str(getattr(tool, "description", "") or ""),
                tool=tool,
                enabled_for_routing=bool(metadata.get("enabled_for_routing", True)),
                when_to_use=str(metadata.get("when_to_use", "") or ""),
                limitations=str(metadata.get("limitations", "") or ""),
            )
            if definition.name in excluded:
                continue
            if not include_non_routable and not definition.enabled_for_routing:
                continue
            registered_tools.append(definition)
    return registered_tools


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    raise RuntimeError("Synchronous FastMCP bridge cannot be called from a running event loop")


__all__ = [
    "FastMCP",
    "ToolDefinition",
    "get_registered_tool",
    "get_server",
    "list_registered_tools",
    "register_server",
]
