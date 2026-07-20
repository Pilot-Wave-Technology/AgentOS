"""MCP server discovery — connects to an MCP server and fetches its tool list.

Two transports are supported:
  - stdio: spawns a local process (command + args)
  - sse:   connects to an HTTP SSE endpoint (URL)

Returns a list of tool defs ready to be registered as mcp_server config.tools.
"""
import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


async def discover_mcp_tools(
    transport: str,
    *,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 30,
) -> list[dict[str, Any]]:
    """Connect to an MCP server and return its tool list.

    Each returned dict has the shape:
      { name, description, input_schema }

    transport must be 'sse' or 'stdio'.
    Raises ValueError for unknown transports or connection failures.
    """
    if transport == "sse":
        return await _discover_via_sse(url, headers=headers, timeout_seconds=timeout_seconds)
    if transport == "stdio":
        return await _discover_via_stdio(command, args or [], env or {}, timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported MCP transport: {transport!r}. Must be 'sse' or 'stdio'")


async def _discover_via_sse(
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    """Connect to MCP server via HTTP SSE transport and call tools/list."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    log.info("MCP discovery: connecting via SSE to %s (timeout=%ds)", url, timeout_seconds)

    async def _do() -> list[dict[str, Any]]:
        async with sse_client(url, headers=headers or {}) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return _normalise_tools(result.tools)

    try:
        tools = await asyncio.wait_for(_do(), timeout=timeout_seconds)
        log.info("MCP discovery: found %d tools at %s", len(tools), url)
        return tools
    except asyncio.TimeoutError:
        log.warning("MCP SSE discovery timed out for %s after %ds", url, timeout_seconds)
        raise ValueError(f"MCP SSE discovery timed out after {timeout_seconds}s")
    except Exception as exc:
        log.warning("MCP SSE discovery failed for %s: %s", url, exc)
        raise ValueError(f"MCP SSE discovery failed: {exc}") from exc


async def _discover_via_stdio(
    command: str,
    args: list[str],
    env: dict[str, str],
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    """Connect to MCP server via stdio transport and call tools/list."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    log.info(
        "MCP discovery: connecting via stdio: %s %s (timeout=%ds)",
        command, " ".join(args), timeout_seconds,
    )

    async def _do() -> list[dict[str, Any]]:
        server_params = StdioServerParameters(command=command, args=args, env=env or None)
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                return _normalise_tools(result.tools)

    try:
        tools = await asyncio.wait_for(_do(), timeout=timeout_seconds)
        log.info("MCP discovery: found %d tools via stdio (%s)", len(tools), command)
        return tools
    except asyncio.TimeoutError:
        log.warning("MCP stdio discovery timed out (%s) after %ds", command, timeout_seconds)
        raise ValueError(f"MCP stdio discovery timed out after {timeout_seconds}s")
    except Exception as exc:
        log.warning("MCP stdio discovery failed (%s): %s", command, exc)
        raise ValueError(f"MCP stdio discovery failed: {exc}") from exc


def _normalise_tools(tools) -> list[dict[str, Any]]:
    """Convert MCP Tool objects to plain dicts with a normalised input_schema."""
    out = []
    for t in tools:
        schema = t.inputSchema
        if hasattr(schema, "model_dump"):
            schema = schema.model_dump(exclude_none=True)
        elif not isinstance(schema, dict):
            try:
                schema = json.loads(schema)
            except Exception:
                schema = {"type": "object", "properties": {}}
        out.append({
            "name": t.name,
            "description": t.description or "",
            "input_schema": schema or {"type": "object", "properties": {}},
        })
    return out
