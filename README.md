# windows-mcp-proxy

A small local stdio MCP server that **fans out a single MCP connection to
many Windows VMs** running [windows-mcp](https://github.com/CursorTouch/Windows-MCP).

Claude Code (or any MCP client) connects once, calls `init` once per project,
and from then on every tool — `click`, `type_text`, `shell`, `screenshot`,
etc. — takes an extra `host` argument that picks which VM the call lands on.

```
                  ┌──────────────────────┐
                  │   Claude Code (CLI)  │
                  └────────┬─────────────┘
                       stdio │  (one subprocess per session)
                  ┌────────▼─────────────┐
                  │  windows-mcp-proxy   │
                  └────┬─────┬─────┬─────┘
                       │     │     │  streamable-http (stateless)
                ┌──────▼─┐ ┌─▼───┐ ┌▼─────┐
                │ dc01   │ │print│ │print │   Windows VMs running
                │ :8765  │ │01   │ │02    │   windows-mcp scheduled task
                └────────┘ └─────┘ └──────┘
```

## Why

- Stock `windows-mcp` is one server, one VM. Registering it in `~/.claude.json`
  pins you to a single IP, and pollutes every project's MCP surface.
- This proxy keeps the MCP wire footprint at **one server per project that
  needs it**, with an inventory of N target VMs reachable through the same
  toolset.

## Install

```bash
pipx install git+https://github.com/techjoec/windows-mcp-proxy.git
# or
uv tool install git+https://github.com/techjoec/windows-mcp-proxy.git
```

## Register per project

Copy `examples/mcp.example.json` to `<project>/.mcp.json`:

```json
{
  "mcpServers": {
    "windows-mcp": {
      "command": "windows-mcp-proxy"
    }
  }
}
```

That's it — projects with no `.mcp.json` entry pay zero token budget.

## Register with Codex

Codex does not currently refresh MCP tools after `notifications/tools/list_changed`,
so use the eager wrapper instead of the lazy default command:

```bash
codex mcp add windows-mcp -- windows-mcp-proxy-codex
```

`windows-mcp-proxy-codex` reads the same inventory file and connects to
`template_host` before Codex asks for the tool list. If the inventory is
missing or `template_host` is unreachable, Codex will see only one setup tool
explaining what to fix. After creating or changing the inventory, restart the
MCP server/Codex session so Codex can list the real tools at startup.

If Codex still exposes only `init`, it is launching the lazy command. Remove
the stale registration and add the eager one again:

```bash
command -v windows-mcp-proxy-codex
codex mcp get windows-mcp
codex mcp remove windows-mcp
codex mcp add windows-mcp -- windows-mcp-proxy-codex
```

If `windows-mcp-proxy` and `windows-mcp-proxy-codex` resolve from different
installs, register the absolute path printed by `command -v`.

As an alternative, the default command can be forced into eager mode:

```bash
codex mcp add windows-mcp \
  --env WINDOWS_MCP_PROXY_EAGER=1 \
  -- windows-mcp-proxy
```

To keep the inventory somewhere other than the default path, register with an
environment override:

```bash
codex mcp add windows-mcp \
  --env WINDOWS_MCP_PROXY_CONFIG=/absolute/path/to/config.json \
  -- windows-mcp-proxy-codex
```

The eager discovery timeout defaults to 10 seconds. Override it with
`WINDOWS_MCP_PROXY_DISCOVERY_TIMEOUT=<seconds>`, or set it to `0` to disable.

## Direct upstream helper

For debugging, `windows-mcp-call` bypasses the proxy MCP surface and calls one
upstream stateless HTTP MCP server from the inventory. It prints JSON, saves
image content to disk, and does not print bearer tokens.

```bash
windows-mcp-call --host print01 --list-tools
windows-mcp-call --host print01 screenshot
windows-mcp-call --host print01 click --args '{"x": 500, "y": 300}'
```

Image content is saved to `/tmp` by default and returned as a file path in the
JSON output. Use `--out-dir <path>` to choose another directory.

## UIA state helpers

Windows-MCP `Screenshot` and `Snapshot` are good for finding the focused
window, visible labels, approximate coordinates, and buttons. For old installer
feature trees, do not treat the text snapshot as reliable for checkbox state,
expand/collapse state, or separate plus/minus versus checkbox glyph coordinates.

After the upstream tools are loaded, this proxy also exposes:

- `proxy_uia_snapshot` — runs a focused-window UI Automation inspection through
  the upstream `PowerShell` tool and returns compact text lines with bounds,
  centers, runtime IDs, `toggle=...`, `expand=...`, and `selected=...`.
- `proxy_uia_action` — uses UIA patterns (`TogglePattern`,
  `ExpandCollapsePattern`, `InvokePattern`, `SelectionItemPattern`) to act on
  a matched element without guessing a coordinate. Prefer a `runtime_id` copied
  from `proxy_uia_snapshot`; use label matchers only when the element is unique.
- `proxy_treeview_snapshot` — reads native `SysTreeView32` controls with Win32
  TreeView messages and reports checkbox state from `TVIS_STATEIMAGEMASK`.
  Use this for InstallShield feature trees that expose expand/select state
  through UIA but do not expose `TogglePattern`.
- `proxy_treeview_action` — changes a native TreeView item and verifies
  before/after state. It supports `check`, `uncheck`, `toggle`, `expand`, and
  `collapse`. Checkbox actions default to selecting the item with
  `TVM_SELECTITEM` and sending Space, which matched the HP InstallShield tree
  better than raw coordinate clicks or posted mouse messages. Set
  `click_method="auto"` or `"input"` to attempt the native state-icon mouse
  path first; the result reports cursor/SendInput diagnostics and falls back to
  the verified keyboard path when the checkbox state does not change.

The UIA helpers stage their PowerShell scripts through `%TEMP%\windows-mcp-proxy`
in small chunks before execution, so they avoid Windows command-line length
limits. Set `WINDOWS_MCP_PROXY_UIA_TIMEOUT=<seconds>` if a very large UI tree
needs more than the default 60 seconds.

The practical flow for fragile tree controls is:

1. Call `Screenshot` or `Snapshot` for visual orientation.
2. Call `proxy_uia_snapshot(host=...)` for stateful text.
3. Call `proxy_treeview_snapshot(host=..., window_title_contains="...")` when
   a `SysTreeView32` feature tree needs checked/unchecked state.
4. Use `proxy_treeview_action(host=..., action="check"|"uncheck",
   text="...")` for native TreeView checkbox changes, then verify with
   `proxy_treeview_snapshot`.
5. Use `proxy_uia_action(host=..., action="toggle"|"expand"|"collapse",
   runtime_id="...")` when the UIA pattern is available.
6. Fall back to coordinate clicks only after confirming the target with a fresh
   screenshot, and verify state afterward. On print05, raw upstream `Click`
   reported success at the HP tree state-icon coordinate while the native
   checkbox state stayed unchanged.

## First run with lazy clients

With the default `windows-mcp-proxy` command, first invocation exposes only
one tool: `init`. Calling it with no inventory file returns the JSON shape you
need to create. Write the file, call `init` again, and the proxy:

1. Connects to `template_host` and lists its tools.
2. Re-exposes each tool with a required `host` argument (enum of your labels).
3. Sends `notifications/tools/list_changed` so the client refreshes.

### Inventory file

Default location: `$PWD/.claude/windows-mcp-proxy/config.json` (per-project).
Set `WINDOWS_MCP_PROXY_CONFIG=/absolute/path/to/config.json` to override it.

```json
{
  "template_host": "print01",
  "hosts": {
    "print01": { "ip": "10.99.0.10", "port": 8765, "bearer_token": "..." },
    "dc01":    { "ip": "10.99.0.2",  "port": 8765, "bearer_token": "..." },
    "print02": { "ip": "10.99.0.11", "port": 8765, "bearer_token": "..." }
  }
}
```

- `template_host` — which VM the proxy queries to discover the tool schema.
  Pick one that's reliably up; the assumption is all VMs run the same
  windows-mcp version.
- `bearer_token` — optional. Omit, or set to `null`, if upstream has no auth.

## How tool calls flow

```
click(host="print01", x=500, y=300)
   │
   ▼
proxy resolves "print01" → 10.99.0.10:8765
   │
   ▼
async with Client("http://10.99.0.10:8765/mcp") as c:
    await c.call_tool_mcp("click", {"x": 500, "y": 300})
```

- One **stateless** HTTP round-trip per call. No persistent connection
  to keep alive (upstream is `--stateless-http`).
- On transport error (`ConnectError`, `ReadTimeout`, `RemoteProtocolError`,
  `ConnectionError`, `ConnectTimeout`) the proxy retries once. If the
  retry also fails it returns:
  > Unable to reconnect to `<label>` (`<ip>`). Check the inventory or
  > the guest VM's windows-mcp process and try again.

## Logging

All logs go to `/logs/windows-mcp-proxy/proxy-<pid>.log`. **Never** stdout —
that is the MCP transport.

Set log level with the env var `WINDOWS_MCP_PROXY_LOG_LEVEL=DEBUG`.

## Multi-session safety

Each Claude Code project that registers the proxy spawns its own stdio
subprocess with its own httpx2 pool. Because upstream is stateless-http,
multiple proxy processes hitting the same VM are independent — no shared
session state to race on.

The expected pattern is **one session ↔ one VM-set**: e.g. session A drives
`{print01, print02}`, session B drives `{lab55}`. Concurrent calls to the
same VM from different sessions are *not* coordinated at the OS level
(that's an upstream concern — windows-mcp's screen state isn't transactional).

## Development

```bash
git clone https://github.com/techjoec/windows-mcp-proxy.git
cd windows-mcp-proxy
pip install -e .

# run directly
python -m windows_mcp_proxy.proxy
```

The two smoke patterns used during development:

1. **No-config path** — launch in a scratch cwd, `list_tools()` returns
   only `init`, calling `init` returns the JSON shape.
2. **End-to-end** — stand up a fake upstream `FastMCP` over HTTP, write a
   config pointing at it, launch the proxy, call `init`, then verify the
   re-exposed tools take `host` and route correctly.

See git history for the actual scripts.

## License

MIT — see `LICENSE`.
