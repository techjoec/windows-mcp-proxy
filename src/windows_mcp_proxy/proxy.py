"""
windows-mcp-proxy
=================

A local stdio MCP that fans out to one or more Windows-MCP HTTP servers
(running inside Windows VMs) and exposes their tools with a `host` argument
prepended, so a single Claude Code session can drive several VMs.

Design notes:

- Stdio subprocess per Claude Code session (registered in project .mcp.json).
- Lazy by default. On launch we expose ONE tool: `init`. After init succeeds
  we discover tools from the inventory's template_host, register them as
  MultiHostProxyTool instances with a `host` enum injected, then notify the
  client of the tool-list change. With WINDOWS_MCP_PROXY_EAGER=1, discovery
  happens before stdio starts for clients that do not support lazy tools.
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
import re
import shutil
import subprocess
import sys
from typing import Any
import uuid

import httpx
import mcp.types as mcp_types
from mcp.shared.exceptions import McpError
from fastmcp import Client, Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.tasks.config import TaskConfig
from fastmcp.tools.tool import Tool, ToolResult
from pydantic import PrivateAttr

from windows_mcp_proxy import uia


# ----- paths & logging --------------------------------------------------------

CONFIG_PATH = Path(
    os.environ.get(
        "WINDOWS_MCP_PROXY_CONFIG",
        Path.cwd() / ".claude" / "windows-mcp-proxy" / "config.json",
    )
).expanduser()
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
#
# Inventory is assembled from two layers:
#   1. Incus instances tagged for this project (the base layer, auto-discovered).
#      An instance joins this session's inventory when its `user.projects` list
#      contains $project_name AND it is a windows-mcp endpoint (`user.windows-mcp.port`
#      set). Coordinates come from user.windows-mcp.{ip,port,bearer}; ip falls back
#      to the instance's live IPv4 if the key is unset.
#   2. The optional config.json overlay (this file) — for hosts Incus does NOT
#      manage: Azure, Tailscale-reached boxes, anything without an Incus instance.
#      Overlay hosts are appended and WIN on label collision.

CONFIG_SHAPE: dict[str, Any] = {
    "template_host": "<optional — a label to discover the tool schema from; defaults to first host>",
    "hosts": {
        "<label>": {
            "ip": "10.x.x.x",
            "port": 8765,
            "bearer_token": "<optional; omit or null if upstream has no auth>",
        }
    },
}

# Incus user keys (see deploy.sh). `user.projects` is the generic per-instance ACL
# (space/comma-separated project slugs), reusable by other incus tooling. The
# windows-mcp.* keys mark + locate a windows-mcp endpoint.
_PROJECTS_KEY = "user.projects"
_WMCP_PORT_KEY = "user.windows-mcp.port"
_WMCP_IP_KEY = "user.windows-mcp.ip"
_WMCP_BEARER_KEY = "user.windows-mcp.bearer"

# Transport errors that warrant a one-shot reconnect+retry.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    ConnectionError,
)

# MCP maps an upstream response/connect timeout to error code 408.
_MCP_TIMEOUT_CODE = 408


def _is_retryable_transport_error(exc: BaseException) -> bool:
    """Whether a failed upstream call deserves one reconnect+retry (and, on a
    repeat failure, the friendly 'unable to reconnect' message).

    FastMCP does not surface raw transport types at the boundary; both failure
    modes were reproduced against a dead host:
      - connection refused -> RuntimeError('Client failed to connect: ...')
        whose __cause__ chain carries httpx.ConnectError
      - connect timeout    -> McpError(code=408)
    So we walk the cause/context/group chain for the known transport types and
    the 408 timeout instead of matching only the outer exception type. A plain
    ValueError or a non-timeout McpError (e.g. method-not-found) returns False
    and is re-raised by the caller.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, _TRANSPORT_ERRORS):
            return True
        if isinstance(e, McpError) and getattr(e.error, "code", None) == _MCP_TIMEOUT_CODE:
            return True
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        if e.__cause__ is not None:
            stack.append(e.__cause__)
        if e.__context__ is not None:
            stack.append(e.__context__)
    return False


def _timeout_client_factory(timeout_seconds: float):
    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["timeout"] = httpx.Timeout(timeout_seconds)
        return httpx.AsyncClient(**kwargs)

    return factory


def _eager_discovery_timeout() -> float | None:
    raw = os.environ.get("WINDOWS_MCP_PROXY_DISCOVERY_TIMEOUT", "10")
    if raw.lower() in {"", "0", "none", "false"}:
        return None
    try:
        timeout = float(raw)
    except ValueError as e:
        raise RuntimeError(
            "WINDOWS_MCP_PROXY_DISCOVERY_TIMEOUT must be a number of seconds, "
            "or 0/none/false to disable."
        ) from e
    if timeout <= 0:
        return None
    return timeout


def _env_truthy(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.lower() in {"1", "true", "yes", "on"}


# ----- runtime state ----------------------------------------------------------

mcp = FastMCP("windows-mcp-proxy")
_config: dict[str, Any] | None = None
_registered_tools: list[str] = []
_upstream_tools: dict[str, mcp_types.Tool] = {}


def _project_slug() -> str | None:
    """This session's project slug. Every lab session exports `project_name`."""
    slug = (os.environ.get("project_name") or os.environ.get("WMCP_PROJECT") or "").strip()
    return slug or None


def _split_slugs(raw: str) -> list[str]:
    """Split a `user.projects` value on whitespace and/or commas."""
    return [s for s in re.split(r"[,\s]+", raw.strip()) if s]


def _instance_ipv4(inst: dict[str, Any]) -> str | None:
    """First global-scope IPv4 from an `incus list --format json` entry's state."""
    net = ((inst.get("state") or {}).get("network")) or {}
    for ifname, idata in net.items():
        if ifname == "lo":
            continue
        for addr in (idata or {}).get("addresses", []) or []:
            if addr.get("family") == "inet" and addr.get("scope") == "global":
                return addr.get("address")
    return None


def _incus_hosts(slug: str) -> dict[str, Any]:
    """Windows-mcp endpoints among Incus instances whose `user.projects` ∋ slug."""
    incus = shutil.which("incus")
    if not incus:
        log.info("incus not on PATH; skipping Incus discovery (overlay only)")
        return {}
    try:
        out = subprocess.run(
            [incus, "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("incus list failed (%s); overlay only", e)
        return {}

    hosts: dict[str, Any] = {}
    for inst in json.loads(out):
        cfg = inst.get("config") or {}
        acl = cfg.get(_PROJECTS_KEY)
        if not acl or slug not in _split_slugs(acl):
            continue
        if _WMCP_PORT_KEY not in cfg:  # tagged for the project but not a windows-mcp endpoint
            continue
        name = inst.get("name")
        ip = cfg.get(_WMCP_IP_KEY) or _instance_ipv4(inst)
        if not ip:
            log.warning("instance %s is a windows-mcp endpoint for %s but has no IP", name, slug)
            continue
        try:
            port = int(cfg.get(_WMCP_PORT_KEY) or 8765)
        except ValueError:
            log.warning("instance %s has non-integer %s; skipping", name, _WMCP_PORT_KEY)
            continue
        hosts[name] = {
            "ip": ip,
            "port": port,
            "bearer_token": cfg.get(_WMCP_BEARER_KEY),
        }
    return hosts


def _load_config() -> dict[str, Any] | None:
    """Assemble inventory: Incus-discovered base + optional config.json overlay."""
    hosts: dict[str, Any] = {}
    slug = _project_slug()
    if slug:
        hosts.update(_incus_hosts(slug))
        log.info("Incus discovery for project=%s -> %d host(s): %s", slug, len(hosts), sorted(hosts))
    else:
        log.info("no `project_name` in env; skipping Incus discovery (overlay only)")

    template: str | None = None
    if CONFIG_PATH.exists():
        try:
            data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError as e:
            log.error("config JSON parse error: %s", e)
            raise RuntimeError(f"Overlay at {CONFIG_PATH} is not valid JSON: {e}") from e
        for label, conf in (data.get("hosts") or {}).items():
            hosts[label] = conf  # overlay wins on collision; adds Incus-unmanaged hosts
        template = data.get("template_host")
        log.info("overlay %s -> hosts now: %s", CONFIG_PATH, sorted(hosts))

    if not hosts:
        return None

    template = os.environ.get("WINDOWS_MCP_PROXY_TEMPLATE_HOST") or template
    if not template or template not in hosts:
        template = next(iter(hosts))
    return {"template_host": template, "hosts": hosts}


def _require_config_for_discovery() -> str:
    """Validate the loaded inventory enough to discover the upstream schema."""
    if _config is None or not _config.get("hosts"):
        slug = _project_slug() or "<unset: export project_name>"
        raise RuntimeError(
            f"No windows-mcp hosts for project '{slug}'. Either tag an Incus instance "
            f"with `{_PROJECTS_KEY}` containing '{slug}' and `{_WMCP_PORT_KEY}`, or add an "
            f"overlay for Incus-unmanaged hosts at {CONFIG_PATH}. Overlay shape:\n\n"
            f"{json.dumps(CONFIG_SHAPE, indent=2)}"
        )

    template = _config.get("template_host")
    if not template or template not in _config["hosts"]:
        raise RuntimeError(
            f"template_host '{template}' is not present in hosts dict "
            f"({list(_config['hosts'])})."
        )
    return template


def _client_for(label: str, *, timeout: float | None = None) -> Client:
    assert _config is not None
    if label not in _config["hosts"]:
        raise ValueError(
            f"Unknown host label '{label}'. Configured: {list(_config['hosts'])}"
        )
    h = _config["hosts"][label]
    url = f"http://{h['ip']}:{h['port']}/mcp"
    token = h.get("bearer_token")
    if token or timeout is not None:
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return Client(
            StreamableHttpTransport(
                url,
                headers=headers,
                httpx_client_factory=(
                    _timeout_client_factory(timeout) if timeout is not None else None
                ),
            )
        )
    return Client(url)


def _find_upstream_tool_name(candidates: list[str]) -> str | None:
    lower_to_name = {name.lower(): name for name in _upstream_tools}
    for candidate in candidates:
        found = lower_to_name.get(candidate.lower())
        if found:
            return found
    return None


def _command_args_for_tool(
    tool_name: str,
    command: str,
    *,
    timeout: int | None = None,
) -> dict[str, Any]:
    override = os.environ.get("WINDOWS_MCP_PROXY_POWERSHELL_ARG")
    if override:
        args = {override: command}
        if timeout is not None:
            args["timeout"] = timeout
        return args

    tool = _upstream_tools.get(tool_name)
    schema = tool.inputSchema if tool else {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []

    for key in ("command", "script", "cmd", "code", "powershell"):
        if key in properties:
            args = {key: command}
            if timeout is not None and "timeout" in properties:
                args["timeout"] = timeout
            return args

    string_required = [
        key
        for key in required
        if isinstance(properties.get(key), dict)
        and properties[key].get("type") in {"string", None}
    ]
    if len(string_required) == 1:
        args = {string_required[0]: command}
        if timeout is not None and "timeout" in properties:
            args["timeout"] = timeout
        return args

    string_props = [
        key
        for key, value in properties.items()
        if isinstance(value, dict) and value.get("type") in {"string", None}
    ]
    if len(string_props) == 1:
        args = {string_props[0]: command}
        if timeout is not None and "timeout" in properties:
            args["timeout"] = timeout
        return args

    raise RuntimeError(
        f"Cannot infer command argument for upstream tool '{tool_name}'. "
        "Set WINDOWS_MCP_PROXY_POWERSHELL_ARG."
    )


def _extract_text(raw: mcp_types.CallToolResult) -> str:
    parts: list[str] = []
    for item in raw.content:
        if isinstance(item, mcp_types.TextContent):
            parts.append(item.text)
    return "\n".join(parts)


def _parse_first_json(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[idx:])
            return payload
        except json.JSONDecodeError:
            continue
    return None


def _extract_json_payload(raw: mcp_types.CallToolResult) -> Any:
    if raw.structuredContent is not None:
        if isinstance(raw.structuredContent, dict):
            if "ok" in raw.structuredContent:
                return raw.structuredContent
            for value in raw.structuredContent.values():
                if isinstance(value, str):
                    parsed = _parse_first_json(value)
                    if parsed is not None:
                        return parsed
        return raw.structuredContent

    text = _extract_text(raw)
    parsed = _parse_first_json(text)
    if parsed is not None:
        return parsed
    return {"ok": False, "error": "No JSON payload found in PowerShell output.", "text": text}


def _powershell_command_for_tool(tool_name: str, script: str) -> str:
    if "powershell" in tool_name.lower():
        return script
    return uia.encode_powershell(script)


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _stage_chunk_size() -> int:
    raw = os.environ.get("WINDOWS_MCP_PROXY_STAGE_CHUNK_SIZE", "1800")
    try:
        size = int(raw)
    except ValueError as e:
        raise RuntimeError("WINDOWS_MCP_PROXY_STAGE_CHUNK_SIZE must be an integer.") from e
    return max(256, min(size, 8000))


def _uia_script_timeout() -> int:
    raw = os.environ.get("WINDOWS_MCP_PROXY_UIA_TIMEOUT", "60")
    try:
        timeout = int(raw)
    except ValueError as e:
        raise RuntimeError("WINDOWS_MCP_PROXY_UIA_TIMEOUT must be an integer.") from e
    return max(1, timeout)


async def _run_powershell_command(
    client: Client,
    tool_name: str,
    script: str,
    *,
    timeout: int | None = None,
) -> mcp_types.CallToolResult:
    command = _powershell_command_for_tool(tool_name, script)
    args = _command_args_for_tool(tool_name, command, timeout=timeout)
    return await client.call_tool_mcp(tool_name, args)


def _raise_on_tool_error(raw: mcp_types.CallToolResult, step: str) -> None:
    if raw.isError:
        text = _extract_text(raw)
        raise RuntimeError(f"PowerShell {step} failed: {text}")


def _remote_temp_expr(name: str) -> str:
    return f"[IO.Path]::Combine($env:TEMP,'windows-mcp-proxy',{_ps_single_quote(name)})"


async def _call_staged_powershell(host: str, tool_name: str, script: str) -> Any:
    script_id = uuid.uuid4().hex
    b64_name = f"uia-{script_id}.b64"
    ps1_name = f"uia-{script_id}.ps1"
    encoded_script = uia.script_base64(script)
    chunk_size = _stage_chunk_size()
    chunks = [
        encoded_script[index:index + chunk_size]
        for index in range(0, len(encoded_script), chunk_size)
    ]

    dir_expr = "[IO.Path]::Combine($env:TEMP,'windows-mcp-proxy')"
    b64_expr = _remote_temp_expr(b64_name)
    ps1_expr = _remote_temp_expr(ps1_name)

    init_script = (
        f"$d={dir_expr};"
        "New-Item -ItemType Directory -Force -Path $d | Out-Null;"
        f"$b={b64_expr};$s={ps1_expr};"
        "Remove-Item -LiteralPath $b,$s -Force -ErrorAction SilentlyContinue;"
        "New-Item -ItemType File -Path $b -Force | Out-Null"
    )
    run_script = (
        f"$b={b64_expr};$s={ps1_expr};"
        "try {"
        "$base64=(-join (Get-Content -LiteralPath $b -Encoding ASCII));"
        "[IO.File]::WriteAllBytes($s,[Convert]::FromBase64String($base64));"
        "& $s"
        "} finally {"
        "Remove-Item -LiteralPath $b,$s -Force -ErrorAction SilentlyContinue"
        "}"
    )

    async with _client_for(host) as c:
        raw = await _run_powershell_command(c, tool_name, init_script, timeout=30)
        _raise_on_tool_error(raw, "stage init")
        for chunk in chunks:
            append_script = (
                f"$b={b64_expr};"
                f"Add-Content -LiteralPath $b -Value {_ps_single_quote(chunk)} -Encoding ASCII"
            )
            raw = await _run_powershell_command(c, tool_name, append_script, timeout=30)
            _raise_on_tool_error(raw, "stage append")

        raw = await _run_powershell_command(
            c,
            tool_name,
            run_script,
            timeout=_uia_script_timeout(),
        )
        _raise_on_tool_error(raw, "script execution")

    return _extract_json_payload(raw)


async def _call_powershell(host: str, script: str) -> Any:
    if _config is None or host not in _config["hosts"]:
        available = sorted(_config["hosts"].keys()) if _config else []
        raise RuntimeError(f"Unknown host '{host}'. Configured labels: {available}")

    tool_name = os.environ.get("WINDOWS_MCP_PROXY_POWERSHELL_TOOL") or _find_upstream_tool_name(
        ["PowerShell", "Shell", "shell", "powershell"]
    )
    if not tool_name:
        raise RuntimeError(
            "No upstream PowerShell/Shell tool was discovered. "
            "Enable the upstream Windows-MCP PowerShell tool."
        )

    return await _call_staged_powershell(host, tool_name, script)


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
            except Exception as e:
                if not _is_retryable_transport_error(e):
                    log.exception("upstream call failed host=%s tool=%s", host, upstream_name)
                    raise
                last_err = e
                log.warning("transport error host=%s tool=%s attempt=%d err=%s",
                            host, upstream_name, attempt, e)
                continue

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
    template = _require_config_for_discovery()

    # Drop anything from a previous init (idempotent re-init).
    for prior in list(_registered_tools):
        try:
            mcp.local_provider.remove_tool(prior)
        except Exception:
            log.debug("could not remove prior tool %s (first init?)", prior)
    _registered_tools.clear()

    upstream_tools = await _list_template_tools(template)
    _register_proxy_tools(mcp, upstream_tools, _registered_tools)
    _register_helper_tools(mcp, _registered_tools)

    # Notify the client to re-list tools so the previously-only-`init` surface
    # gets replaced with the real tool set.
    try:
        await ctx.send_notification(mcp_types.ToolListChangedNotification())
    except Exception:
        log.exception("could not send tool-list-changed notification (non-fatal)")

    log.info("registered %d tools from %s: %s",
             len(_registered_tools), template, _registered_tools)
    return len(_registered_tools), list(_registered_tools)


async def _list_template_tools(
    template: str,
    *,
    timeout: float | None = None,
) -> list[mcp_types.Tool]:
    async with _client_for(template, timeout=timeout) as c:
        return await c.list_tools()


def _register_proxy_tools(
    server: FastMCP,
    upstream_tools: list[mcp_types.Tool],
    registered_tools: list[str],
) -> None:
    _upstream_tools.clear()
    for t in upstream_tools:
        _upstream_tools[t.name] = t
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
        server.add_tool(tool)
        registered_tools.append(t.name)


def _format_bounds(bounds: dict[str, Any] | None) -> str:
    if not bounds:
        return "bounds=?"
    return (
        f"bounds=({bounds.get('left')},{bounds.get('top')},"
        f"{bounds.get('right')},{bounds.get('bottom')}) "
        f"center=({bounds.get('centerX')},{bounds.get('centerY')})"
    )


def _format_patterns(patterns: dict[str, Any] | None) -> str:
    if not patterns:
        return "patterns=none"

    parts: list[str] = []
    toggle = patterns.get("toggle")
    if isinstance(toggle, dict):
        parts.append(f"toggle={toggle.get('state')}")
    expand = patterns.get("expandCollapse")
    if isinstance(expand, dict):
        parts.append(f"expand={expand.get('state')}")
    selection = patterns.get("selectionItem")
    if isinstance(selection, dict):
        parts.append(f"selected={selection.get('isSelected')}")
    if "invoke" in patterns:
        parts.append("invoke=true")
    if "value" in patterns:
        value = patterns.get("value", {})
        if isinstance(value, dict) and value.get("value"):
            parts.append(f"value={value.get('value')!r}")
    return " ".join(parts) if parts else "patterns=none"


def _format_uia_tree(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"UIA snapshot returned non-object payload:\n{payload}"
    if not payload.get("ok"):
        return f"UIA snapshot failed: {payload.get('error', payload)}"

    lines = [
        "UIA snapshot of focused window",
        (
            "State source: System.Windows.Automation patterns. Use toggle/expand/"
            "selected fields as state; plain Windows-MCP text labels are only "
            "navigation hints."
        ),
        (
            "Coordinate note: bounds are whole controls. Separate checkbox or "
            "plus/minus glyph coordinates are shown only if Windows exposes "
            "those glyphs as child controls."
        ),
        f"focusedRuntimeId={payload.get('focusedRuntimeId')}",
        f"nodeCount={payload.get('nodeCount')} truncated={payload.get('truncated')}",
        "",
    ]

    ordinal = 0

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal ordinal
        ordinal += 1
        indent = "  " * depth
        name = node.get("name") or ""
        control = node.get("controlType") or node.get("localizedControlType") or "Control"
        runtime_id = node.get("runtimeId") or "?"
        details = [
            f"[{ordinal}]",
            control,
            repr(name),
            _format_bounds(node.get("bounds")),
            _format_patterns(node.get("patterns")),
            f"runtimeId={runtime_id}",
        ]
        if node.get("automationId"):
            details.append(f"automationId={node.get('automationId')!r}")
        if node.get("className"):
            details.append(f"className={node.get('className')!r}")
        lines.append(indent + " ".join(details))
        for child in node.get("children", []) or []:
            if isinstance(child, dict):
                walk(child, depth + 1)

    root = payload.get("root")
    if isinstance(root, dict):
        walk(root, 0)
    else:
        lines.append("No root node returned.")
    return "\n".join(lines)


def _action_criteria(
    runtime_id: str | None,
    name: str | None,
    name_contains: str | None,
    automation_id: str | None,
    class_name: str | None,
    control_type: str | None,
    index: int,
) -> dict[str, Any]:
    criteria: dict[str, Any] = {"index": index}
    if runtime_id:
        criteria["runtimeId"] = runtime_id
    if name:
        criteria["name"] = name
    if name_contains:
        criteria["nameContains"] = name_contains
    if automation_id:
        criteria["automationId"] = automation_id
    if class_name:
        criteria["className"] = class_name
    if control_type:
        criteria["controlType"] = control_type
    return criteria


def _format_uia_action(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"UIA action returned non-object payload:\n{payload}"
    if not payload.get("ok"):
        return f"UIA action failed: {payload.get('error', payload)}"

    before = payload.get("before", {})
    after = payload.get("after", {})
    return "\n".join(
        [
            f"UIA action {payload.get('action')} completed.",
            f"matchCount={payload.get('matchCount')} selectedIndex={payload.get('selectedIndex')}",
            f"before: {before.get('controlType')} {before.get('name')!r} state={before.get('state')} {_format_bounds(before.get('bounds'))} runtimeId={before.get('runtimeId')}",
            f"after:  {after.get('controlType')} {after.get('name')!r} state={after.get('state')} {_format_bounds(after.get('bounds'))} runtimeId={after.get('runtimeId')}",
        ]
    )


def _format_treeview_snapshot(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"TreeView snapshot returned non-object payload:\n{payload}"
    if not payload.get("ok"):
        return f"TreeView snapshot failed: {payload.get('error', payload)}"

    treeviews = payload.get("treeviews") or []
    lines = [
        "Win32 SysTreeView32 snapshot",
        (
            "State source: TVM_GETITEM/TVIS_STATEIMAGEMASK. This can report "
            "checkbox state for common TreeView checkboxes even when UIA has "
            "no TogglePattern."
        ),
        "State mapping: checkbox=unchecked means stateImageIndex=1; checked=2; mixed=3; none=0.",
        f"treeviewCount={len(treeviews)} windowTitleContains={payload.get('windowTitleContains')!r}",
        "",
    ]

    def walk(items: list[Any], depth: int) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            indent = "  " * depth
            text = item.get("text") or ""
            state = item.get("checkboxState")
            state_index = item.get("stateImageIndex")
            handle = item.get("handle")
            details = [
                f"[{item.get('index')}]",
                repr(text),
                f"checkbox={state}",
                f"stateImageIndex={state_index}",
                _format_bounds(item.get("bounds")),
                f"handle={handle}",
            ]
            lines.append(indent + " ".join(details))
            children = item.get("children") or []
            if isinstance(children, list):
                walk(children, depth + 1)

    for tree_index, tree in enumerate(treeviews, start=1):
        if not isinstance(tree, dict):
            continue
        lines.append(
            f"TreeView[{tree_index}] hwnd={tree.get('hwnd')} "
            f"topTitle={tree.get('topTitle')!r} processId={tree.get('processId')} "
            f"itemCount={tree.get('itemCount')} truncated={tree.get('truncated')}"
        )
        if tree.get("error"):
            lines.append(f"  error={tree.get('error')}")
            continue
        items = tree.get("items") or []
        if isinstance(items, list):
            walk(items, 1)
        lines.append("")

    if len(treeviews) == 0:
        lines.append("No visible SysTreeView32 controls found.")
    return "\n".join(lines).rstrip()


def _format_treeview_item_state(item: dict[str, Any]) -> str:
    return (
        f"{item.get('text')!r} checkbox={item.get('checkboxState')} "
        f"stateImageIndex={item.get('stateImageIndex')} "
        f"{_format_bounds(item.get('bounds'))} handle={item.get('handle')}"
    )


def _format_treeview_click(click: Any) -> str | None:
    if not isinstance(click, dict):
        return None
    focus = click.get("focus") if isinstance(click.get("focus"), dict) else {}
    return (
        f"click method={click.get('method')} "
        f"setCursorPosOk={click.get('setCursorPosOk')} "
        f"setCursorPosError={click.get('setCursorPosError')} "
        f"sentInputs={click.get('sentInputs')} "
        f"sendInputError={click.get('sendInputError')} "
        f"postedMessages={click.get('postedMessages')} "
        f"cursorBefore=({click.get('cursorBeforeX')},{click.get('cursorBeforeY')}) "
        f"cursorAfter=({click.get('cursorAfterX')},{click.get('cursorAfterY')}) "
        f"foreground={focus.get('beforeForeground')}->{focus.get('afterForeground')} "
        f"threads current/target/foreground="
        f"{focus.get('currentThread')}/{focus.get('targetThread')}/{focus.get('foregroundThread')}"
    )


def _format_treeview_action(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"TreeView action returned non-object payload:\n{payload}"
    if not payload.get("ok"):
        if isinstance(payload.get("before"), dict) and isinstance(payload.get("after"), dict):
            hit = payload.get("hit") or {}
            lines = [
                f"TreeView action {payload.get('action')} did not reach desired state.",
                (
                    f"attempted={payload.get('attempted')} clickMethod={payload.get('clickMethod')} "
                    f"effectiveClickMethod={payload.get('effectiveClickMethod')} "
                    f"fallbackAttempted={payload.get('fallbackAttempted')} "
                    f"matchCount={payload.get('matchCount')} selectedIndex={payload.get('selectedIndex')}"
                ),
                (
                    f"hit found={hit.get('found')} client=({hit.get('clientX')},{hit.get('clientY')}) "
                    f"screen=({hit.get('screenX')},{hit.get('screenY')}) flags={hit.get('flags')}"
                ),
            ]
            click_line = _format_treeview_click(payload.get("click"))
            if click_line:
                lines.append(click_line)
            if isinstance(payload.get("afterMouse"), dict):
                lines.append(f"afterMouse: {_format_treeview_item_state(payload['afterMouse'])}")
            lines.extend([
                f"before: {_format_treeview_item_state(payload['before'])}",
                f"after:  {_format_treeview_item_state(payload['after'])}",
            ])
            return "\n".join(lines)
        before = payload.get("before")
        suffix = ""
        if isinstance(before, dict):
            suffix = f"\nbefore: {_format_treeview_item_state(before)}"
        return f"TreeView action failed: {payload.get('error', payload)}{suffix}"

    before = payload.get("before") or {}
    after = payload.get("after") or {}
    hit = payload.get("hit") or {}
    tree = payload.get("tree") or {}
    return "\n".join([
        f"TreeView action {payload.get('action')} completed.",
        (
            f"attempted={payload.get('attempted')} note={payload.get('note')} "
            f"clickMethod={payload.get('clickMethod')} "
            f"effectiveClickMethod={payload.get('effectiveClickMethod')} "
            f"fallbackAttempted={payload.get('fallbackAttempted')} "
            f"matchCount={payload.get('matchCount')} selectedIndex={payload.get('selectedIndex')}"
        ),
        (
            f"tree hwnd={tree.get('hwnd')} topTitle={tree.get('topTitle')!r} "
            f"processId={tree.get('processId')}"
        ),
        (
            f"hit found={hit.get('found')} client=({hit.get('clientX')},{hit.get('clientY')}) "
            f"screen=({hit.get('screenX')},{hit.get('screenY')}) flags={hit.get('flags')}"
        ),
        *([click_line] if (click_line := _format_treeview_click(payload.get("click"))) else []),
        *(
            [f"afterMouse: {_format_treeview_item_state(payload['afterMouse'])}"]
            if isinstance(payload.get("afterMouse"), dict)
            else []
        ),
        f"before: {_format_treeview_item_state(before)}",
        f"after:  {_format_treeview_item_state(after)}",
    ])


def _register_helper_tools(server: FastMCP, registered_tools: list[str]) -> None:
    @server.tool(
        name="proxy_uia_snapshot",
        description=(
            "Inspect the focused Windows window with UI Automation and return a "
            "compact text tree with reliable TogglePattern, ExpandCollapsePattern, "
            "SelectionItemPattern state, bounds, centers, and runtime IDs."
        ),
    )
    async def proxy_uia_snapshot(
        host: str,
        max_depth: int = 6,
        max_nodes: int = 300,
        include_offscreen: bool = False,
    ) -> str:
        max_depth = max(0, min(max_depth, 12))
        max_nodes = max(1, min(max_nodes, 1000))
        payload = await _call_powershell(
            host,
            uia.snapshot_script(
                max_depth=max_depth,
                max_nodes=max_nodes,
                include_offscreen=include_offscreen,
            ),
        )
        return _format_uia_tree(payload)

    @server.tool(
        name="proxy_uia_action",
        description=(
            "Act on a focused-window UI Automation element by pattern instead "
            "of coordinate clicks. Supports toggle, expand, collapse, invoke, "
            "and select. Prefer runtime_id copied from proxy_uia_snapshot."
        ),
    )
    async def proxy_uia_action(
        host: str,
        action: str,
        runtime_id: str | None = None,
        name: str | None = None,
        name_contains: str | None = None,
        automation_id: str | None = None,
        class_name: str | None = None,
        control_type: str | None = None,
        index: int = 1,
        max_depth: int = 8,
        max_nodes: int = 500,
    ) -> str:
        action = action.lower()
        if action not in {"toggle", "expand", "collapse", "invoke", "select"}:
            return "Unsupported action. Use toggle, expand, collapse, invoke, or select."
        if not any([runtime_id, name, name_contains, automation_id, class_name, control_type]):
            return (
                "Provide runtime_id from proxy_uia_snapshot, or at least one "
                "matcher such as name_contains/control_type."
            )

        criteria = _action_criteria(
            runtime_id,
            name,
            name_contains,
            automation_id,
            class_name,
            control_type,
            max(1, index),
        )
        payload = await _call_powershell(
            host,
            uia.action_script(
                action=action,
                criteria=criteria,
                max_depth=max(0, min(max_depth, 12)),
                max_nodes=max(1, min(max_nodes, 1000)),
            ),
        )
        return _format_uia_action(payload)

    @server.tool(
        name="proxy_treeview_snapshot",
        description=(
            "Read native Win32 SysTreeView32 items with TVM_GETITEM and report "
            "TreeView checkbox state from TVIS_STATEIMAGEMASK. Use this when "
            "UIA lacks TogglePattern for installer feature trees."
        ),
    )
    async def proxy_treeview_snapshot(
        host: str,
        window_title_contains: str | None = None,
        max_nodes: int = 500,
    ) -> str:
        max_nodes = max(1, min(max_nodes, 2000))
        payload = await _call_powershell(
            host,
            uia.treeview_script(
                window_title_contains=window_title_contains,
                max_nodes=max_nodes,
            ),
        )
        return _format_treeview_snapshot(payload)

    @server.tool(
        name="proxy_treeview_action",
        description=(
            "Change or expand/collapse a native Win32 SysTreeView32 item and "
            "verify before/after state. For checkboxes, finds the native "
            "TVHT_ONITEMSTATEICON hit target before sending the TreeView mouse "
            "input path, or uses a verified TreeView keyboard path. Supports "
            "toggle, check, uncheck, expand, collapse."
        ),
    )
    async def proxy_treeview_action(
        host: str,
        action: str,
        window_title_contains: str | None = None,
        text: str | None = None,
        text_contains: str | None = None,
        handle: str | None = None,
        index: int = 1,
        max_nodes: int = 500,
        click_method: str = "keyboard",
    ) -> str:
        action = action.lower()
        if action not in {"toggle", "check", "uncheck", "expand", "collapse"}:
            return "Unsupported action. Use toggle, check, uncheck, expand, or collapse."
        if not any([text, text_contains, handle]):
            return "Provide handle, text, or text_contains to identify the TreeView item."
        click_method = click_method.lower()
        if click_method not in {"auto", "message", "input", "keyboard"}:
            return "Unsupported click_method. Use auto, message, input, or keyboard."

        payload = await _call_powershell(
            host,
            uia.treeview_action_script(
                action=action,
                window_title_contains=window_title_contains,
                text=text,
                text_contains=text_contains,
                handle=handle,
                index=max(1, index),
                max_nodes=max(1, min(max_nodes, 2000)),
                click_method=click_method,
            ),
        )
        return _format_treeview_action(payload)

    registered_tools.extend([
        "proxy_uia_snapshot",
        "proxy_uia_action",
        "proxy_treeview_snapshot",
        "proxy_treeview_action",
    ])


async def configure_eager_server(server: FastMCP) -> tuple[int, list[str]]:
    """Register all upstream tools before the MCP client asks for tools.

    This is used by clients that do not support dynamic/deferred tool loading.
    """
    global _config
    _config = _load_config()
    template = _require_config_for_discovery()
    upstream_tools = await _list_template_tools(
        template,
        timeout=_eager_discovery_timeout(),
    )
    registered_tools: list[str] = []
    _register_proxy_tools(server, upstream_tools, registered_tools)
    _register_helper_tools(server, registered_tools)
    log.info(
        "eagerly registered %d tools from %s: %s",
        len(registered_tools),
        template,
        registered_tools,
    )
    return len(registered_tools), registered_tools


SETUP_DESCRIPTION = """\
Show why the eager windows-mcp proxy did not expose remote-control tools at
startup.

Eager mode must read the inventory and reach template_host before the MCP
client asks for the tool list. After fixing the inventory or VM, restart the
MCP server so the client can see the real tools.
"""


def register_setup_tool(
    server: FastMCP,
    startup_error: str,
    *,
    server_label: str = "windows-mcp-proxy",
) -> None:
    @server.tool(name="setup_windows_mcp_proxy", description=SETUP_DESCRIPTION)
    async def setup_windows_mcp_proxy() -> str:
        return (
            f"{server_label} could not load the remote-control tools.\n\n"
            f"Reason:\n{startup_error}\n\n"
            "Eager mode does not defer tool registration. Create or fix "
            f"the inventory at {CONFIG_PATH}, confirm template_host is "
            "reachable, then restart this MCP server.\n\n"
            "Expected inventory shape:\n"
            f"{json.dumps(CONFIG_SHAPE, indent=2)}"
        )


async def configure_eager_or_setup_server(
    server: FastMCP,
    *,
    server_label: str = "windows-mcp-proxy",
) -> tuple[bool, int, list[str]]:
    try:
        count, names = await configure_eager_server(server)
    except Exception as e:
        log.exception("eager startup failed")
        register_setup_tool(server, str(e), server_label=server_label)
        return False, 0, []

    log.info("eager wrapper ready with %d tools: %s", count, names)
    return True, count, names


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
    except Exception as e:
        if _is_retryable_transport_error(e):
            template = _config.get("template_host")
            ip = _config["hosts"].get(template, {}).get("ip", "?") if template else "?"
            log.error("init: cannot reach template_host %s (%s): %s", template, ip, e)
            return (
                f"Could not reach template_host '{template}' ({ip}) to discover tools: {e}\n"
                f"Confirm the windows-mcp scheduled task is running on that VM, then call `init` again."
            )
        log.exception("init failed")
        return f"init failed: {e}"

    return (
        f"Loaded {count} tools from template_host '{_config['template_host']}'.\n"
        f"Hosts available via the `host` arg: {sorted(_config['hosts'].keys())}\n"
        f"Tools: {names}"
    )


# ----- entry point ------------------------------------------------------------

def main() -> None:
    if _env_truthy("WINDOWS_MCP_PROXY_EAGER"):
        try:
            mcp.local_provider.remove_tool("init")
        except Exception:
            log.debug("init tool was not registered before eager startup")
        import asyncio

        asyncio.run(
            configure_eager_or_setup_server(
                mcp,
                server_label=Path(sys.argv[0]).name or "windows-mcp-proxy",
            )
        )

    try:
        mcp.run(show_banner=False)
    except Exception:
        log.exception("fatal in mcp.run()")
        raise


if __name__ == "__main__":
    main()
