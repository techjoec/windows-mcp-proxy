"""
windows-mcp-proxy
=================

A local stdio MCP that fans out to one or more Windows-MCP HTTP servers
(running inside Windows VMs) and exposes their tools with a `host` argument
prepended, so a single Claude Code session can drive several VMs.

Design notes:

- Stdio subprocess per Claude Code session (registered in project .mcp.json).
- Lazy everything. On launch we expose ONE tool: `init`. After init succeeds
  we discover tools from the inventory's template_host, register them as
  MultiHostProxyTool instances with a `host` enum injected, then notify the
  client of the tool-list change.
- No persistent client pool. Each call opens an `async with Client(...)`;
  upstream is stateless-http so this is effectively free (httpx pools sockets).
- Retry-once on transport error, then return a fixed user-facing message.
- All logging to /logs/windows-mcp-proxy/proxy-<pid>.log. NEVER stdout
  (that is the MCP transport).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import mcp.types as mcp_types
from fastmcp import Client, Context, FastMCP
from fastmcp.server.tasks.config import TaskConfig
from fastmcp.tools.tool import Tool, ToolResult
from pydantic import PrivateAttr


# ----- paths & logging --------------------------------------------------------

CONFIG_PATH = Path.cwd() / ".claude" / "windows-mcp-proxy" / "config.json"
LOG_DIR = Path("/logs/windows-mcp-proxy")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(LOG_DIR / f"proxy-{os.getpid()}.log"),
    level=os.environ.get("WINDOWS_MCP_PROXY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("windows-mcp-proxy")
log.info("proxy starting; pid=%s cwd=%s config=%s", os.getpid(), Path.cwd(), CONFIG_PATH)


# ----- inventory shape (shown to the model when config is missing) -----------

CONFIG_SHAPE: dict[str, Any] = {
    "template_host": "<one of the labels below — used to discover the tool schema>",
    "hosts": {
        "<label>": {
            "ip": "10.x.x.x",
            "port": 8765,
            "bearer_token": "<optional; omit or null if upstream has no auth>",
        }
    },
}

# Transport errors that warrant a one-shot reconnect+retry.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    ConnectionError,
)


# ----- runtime state ----------------------------------------------------------

mcp = FastMCP("windows-mcp-proxy")
_config: dict[str, Any] | None = None
_registered_tools: list[str] = []


def _load_config() -> dict[str, Any] | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
        return data
    except json.JSONDecodeError as e:
        log.error("config JSON parse error: %s", e)
        raise RuntimeError(f"Inventory at {CONFIG_PATH} is not valid JSON: {e}") from e


def _client_for(label: str) -> Client:
    assert _config is not None
    if label not in _config["hosts"]:
        raise ValueError(
            f"Unknown host label '{label}'. Configured: {list(_config['hosts'])}"
        )
    h = _config["hosts"][label]
    url = f"http://{h['ip']}:{h['port']}/mcp"
    token = h.get("bearer_token")
    if token:
        return Client(url, headers={"Authorization": f"Bearer {token}"})
    return Client(url)


# ----- multi-host proxy tool --------------------------------------------------

class MultiHostProxyTool(Tool):
    """A Tool that forwards to one of N upstream Windows-MCP servers, selected
    by the `host` argument that this proxy injects into the input schema."""

    task_config: TaskConfig = TaskConfig(mode="forbidden")
    _upstream_name: str = PrivateAttr(default="")

    @classmethod
    def build(cls, *, upstream_name: str, **kwargs: Any) -> MultiHostProxyTool:
        inst = cls(**kwargs)
        inst._upstream_name = upstream_name
        return inst

    async def run(self, arguments: dict[str, Any]) -> ToolResult:  # type: ignore[override]
        args = dict(arguments)
        host = args.pop("host", None)
        upstream_name = self._upstream_name or self.name

        if _config is None or host is None or host not in _config["hosts"]:
            available = sorted(_config["hosts"].keys()) if _config else []
            return ToolResult(
                content=[mcp_types.TextContent(
                    type="text",
                    text=f"Unknown host '{host}'. Configured labels: {available}",
                )]
            )

        last_err: BaseException | None = None
        for attempt in (1, 2):
            try:
                async with _client_for(host) as c:
                    log.debug("call %s/%s attempt=%d args=%s",
                              host, upstream_name, attempt, args)
                    raw = await c.call_tool_mcp(upstream_name, args)
                return ToolResult(
                    content=list(raw.content),
                    structured_content=raw.structuredContent,
                )
            except _TRANSPORT_ERRORS as e:
                last_err = e
                log.warning("transport error host=%s tool=%s attempt=%d err=%s",
                            host, upstream_name, attempt, e)
                continue
            except Exception:
                log.exception("upstream call failed host=%s tool=%s", host, upstream_name)
                raise

        ip = _config["hosts"][host]["ip"]
        log.error("giving up on %s after retry: %s", host, last_err)
        return ToolResult(
            content=[mcp_types.TextContent(
                type="text",
                text=(
                    f"Unable to reconnect to {host} ({ip}). "
                    f"Check the inventory or the guest VM's windows-mcp process and try again."
                ),
            )]
        )


# ----- discovery + (re)registration ------------------------------------------

def _inject_host_arg(upstream_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of the upstream JSON Schema with `host` prepended as a
    required string with an enum of configured labels."""
    assert _config is not None
    schema: dict[str, Any] = dict(upstream_schema or {})
    schema.setdefault("type", "object")
    upstream_props: dict[str, Any] = dict(schema.get("properties", {}))
    upstream_required: list[str] = list(schema.get("required", []))

    new_props: dict[str, Any] = {
        "host": {
            "type": "string",
            "enum": sorted(_config["hosts"].keys()),
            "description": (
                "Target host label from the inventory in "
                ".claude/windows-mcp-proxy/config.json"
            ),
        }
    }
    new_props.update(upstream_props)
    schema["properties"] = new_props
    schema["required"] = ["host", *upstream_required]
    return schema


async def _discover_and_register(ctx: Context) -> tuple[int, list[str]]:
    """Discover upstream tools from template_host and register them locally."""
    assert _config is not None
    template = _config.get("template_host")
    if not template:
        raise RuntimeError("Inventory is missing required 'template_host' key.")
    if template not in _config["hosts"]:
        raise RuntimeError(
            f"template_host '{template}' is not present in hosts dict "
            f"({list(_config['hosts'])})."
        )

    # Drop anything from a previous init (idempotent re-init).
    for prior in list(_registered_tools):
        try:
            mcp.remove_tool(prior)
        except Exception:
            log.debug("could not remove prior tool %s (first init?)", prior)
    _registered_tools.clear()

    async with _client_for(template) as c:
        upstream_tools = await c.list_tools()

    for t in upstream_tools:
        injected_schema = _inject_host_arg(t.inputSchema)
        tool = MultiHostProxyTool.build(
            upstream_name=t.name,
            name=t.name,
            description=(
                (t.description or "")
                + "\n\n(Proxied from windows-mcp; `host` selects the target VM.)"
            ),
            parameters=injected_schema,
        )
        mcp.add_tool(tool)
        _registered_tools.append(t.name)

    # Notify the client to re-list tools so the previously-only-`init` surface
    # gets replaced with the real tool set.
    try:
        await ctx.send_notification(mcp_types.ToolListChangedNotification())
    except Exception:
        log.exception("could not send tool-list-changed notification (non-fatal)")

    log.info("registered %d tools from %s: %s",
             len(_registered_tools), template, _registered_tools)
    return len(_registered_tools), list(_registered_tools)


# ----- the single bootstrap tool ---------------------------------------------

INIT_DESCRIPTION = """\
Initialize the windows-mcp-proxy.

This proxy exposes Windows GUI / shell remote-control tools for one or more
Windows VMs. On first launch only this `init` tool is visible — call it to
load the per-project inventory and expose the real tools (click, type, shell,
screenshot, etc.) with a `host` argument selecting which VM to target.

Inventory file: $PWD/.claude/windows-mcp-proxy/config.json

If the file does not exist, `init` returns the JSON shape to write. After
writing it, call `init` again. After successful init, the proxy sends a
tools/list_changed notification and the real tools appear.
"""


@mcp.tool(description=INIT_DESCRIPTION)
async def init(ctx: Context) -> str:
    global _config
    try:
        _config = _load_config()
    except RuntimeError as e:
        return str(e)

    if _config is None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log.info("no config; advertising shape to model")
        return (
            f"No inventory found at {CONFIG_PATH}.\n\n"
            f"Create it with this shape (replace <placeholders>):\n\n"
            f"{json.dumps(CONFIG_SHAPE, indent=2)}\n\n"
            f"Then call `init` again. The proxy will connect to template_host, "
            f"discover its tools, and re-expose them with a `host` argument."
        )

    if "hosts" not in _config or not _config["hosts"]:
        return (
            f"Inventory at {CONFIG_PATH} has no `hosts`. "
            f"Expected shape:\n\n{json.dumps(CONFIG_SHAPE, indent=2)}"
        )

    try:
        count, names = await _discover_and_register(ctx)
    except _TRANSPORT_ERRORS as e:
        template = _config.get("template_host")
        ip = _config["hosts"].get(template, {}).get("ip", "?") if template else "?"
        log.error("init: cannot reach template_host %s (%s): %s", template, ip, e)
        return (
            f"Could not reach template_host '{template}' ({ip}) to discover tools: {e}\n"
            f"Confirm the windows-mcp scheduled task is running on that VM, then call `init` again."
        )
    except Exception as e:
        log.exception("init failed")
        return f"init failed: {e}"

    return (
        f"Loaded {count} tools from template_host '{_config['template_host']}'.\n"
        f"Hosts available via the `host` arg: {sorted(_config['hosts'].keys())}\n"
        f"Tools: {names}"
    )


# ----- entry point ------------------------------------------------------------

def main() -> None:
    try:
        mcp.run(show_banner=False)
    except Exception:
        log.exception("fatal in mcp.run()")
        raise


if __name__ == "__main__":
    main()
