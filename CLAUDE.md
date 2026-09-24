# CLAUDE.md — windows-mcp-proxy

Notes for future Claude Code sessions working on this repo.

## What this is

A local stdio MCP that **proxies one MCP client connection to N upstream
windows-mcp HTTP servers**, selected per-call by a `host` argument injected
into every tool's input schema.

Registered per-project in `<project>/.mcp.json`. Inventory per-project in
`<project>/.claude/windows-mcp-proxy/config.json`. Logs at
`/logs/windows-mcp-proxy/proxy-<pid>.log`.

User-facing details + diagrams live in `README.md` — don't duplicate.

## Architecture in one paragraph

`FastMCP("windows-mcp-proxy")` exposes a single bootstrap tool `init`. When
the model calls `init`, the proxy reads its inventory, opens a `Client` to
`template_host`, calls `list_tools()`, and for each upstream tool registers
a `MultiHostProxyTool` (subclass of `fastmcp.tools.tool.Tool`) whose
`parameters` is the upstream `inputSchema` with `host` (a string enum of
configured labels) injected as a required property. Then it sends
`ToolListChangedNotification`. On call, `run()` pops `host`, opens a fresh
`async with Client(url)` for that host, calls `call_tool_mcp(upstream_name,
args)`, and wraps the result as `ToolResult`. Transport errors get one
retry; further failure returns a fixed user-facing message.

## Why this shape

- **Lazy everything.** Starting with N×M tools at boot would bloat every
  session's context. One `init` tool is ~1 paragraph of description; the
  real toolset only materializes after the model decides this project
  actually needs it.
- **Per-project inventory.** Different labs / projects have different VMs,
  bearer tokens, and IP ranges. Storing inventory in `$PWD/.claude/...`
  keeps lab boundaries clean and lets the file be (optionally) committed.
- **Stateless-http upstream.** No persistent client pool — `async with
  Client(...)` per call is effectively free because httpx pools sockets
  underneath. Reconnect == next call.
- **Retry-once, not ping-then-call.** Ping before each call would double
  round trips for no benefit when upstream is stateless. Same failure
  surface, half the latency.

## FastMCP 3.3.1 API gotchas (verified against installed source 2026-05-19)

These are the ones that bit during build:

- `FunctionTool.from_function()` does **not** accept a `parameters` kwarg.
  Schemas are introspected from Python type hints. To register a tool
  whose JSON Schema is only known at runtime, **subclass `Tool` directly**
  and pass `parameters=...` to the model constructor. That's what
  `MultiHostProxyTool` does.
- `TaskConfig` moved: import from `fastmcp.server.tasks.config`, not
  `fastmcp.tools.tool`. The old import emits a private-import warning.
- `Tool.run(arguments)` takes only `arguments` (no `context` kwarg in the
  parent class). `ProxyTool.run` adds a `context=None` but the dispatcher
  is forgiving. We match the parent signature.
- `ctx.send_notification(mcp_types.ToolListChangedNotification())` works
  in stdio sessions — fastmcp delivers it on the current session, which
  is exactly what we want (1 stdio subprocess = 1 session).
- `mcp.run(show_banner=False)` is mandatory for stdio — the Rich banner
  is fine on stderr but `show_banner=True` also taints startup ordering
  in some shells.
- Private attrs on `Tool` subclasses need `pydantic.PrivateAttr`. We use
  this for `_upstream_name` so the original (un-injected) name is
  available in `run()`.

## Files

- `src/windows_mcp_proxy/proxy.py` — the lazy server and shared proxy
  mechanics. Worth keeping the core mechanics here; if it grows, split
  *transport*, *dispatch*, and *inventory* but not before.
- `src/windows_mcp_proxy/codex.py` — Codex-compatible eager wrapper. It
  imports the shared helpers from `proxy.py`, discovers/registers all
  upstream tools before stdio starts, and exposes a setup-only tool if eager
  discovery fails.
- `src/windows_mcp_proxy/direct.py` — `windows-mcp-call`, a direct upstream
  debugging helper that reads the inventory, calls one tool, saves image
  content to disk, and avoids printing bearer tokens.
- `src/windows_mcp_proxy/uia.py` — PowerShell UI Automation scripts for the
  proxy-owned `proxy_uia_snapshot`, `proxy_uia_action`, and
  `proxy_treeview_snapshot` helper tools.
- `pyproject.toml` — `windows-mcp-proxy` entry point points at
  `proxy:main`; `windows-mcp-proxy-codex` points at `codex:main`;
  `windows-mcp-call` points at `direct:main`. Depends on `fastmcp>=3.3,<4`
  (`<4` because fastmcp 4 pulls mcp 2, where `mcp.shared.exceptions.McpError`
  is gone — `proxy.py` fails at import; the port is in `INBOX.md`).
- `README.md` — user-facing.
- `CLAUDE.md` — this file.
- `LICENSE` — MIT.

No tests directory yet — smoke tests during development used a scratch
fake-upstream `FastMCP` over HTTP. If we add a test suite, port that
pattern into `tests/`.

## When upstream windows-mcp changes

- **New tool added upstream** → call `init` again. The proxy is
  idempotent: it `remove_tool`s the previous set and re-registers.
  For Codex, restart the MCP server/session because Codex does not consume
  lazy tool-list updates. If Codex is stuck with only `init`, check that it
  is registered with `windows-mcp-proxy-codex` or with
  `WINDOWS_MCP_PROXY_EAGER=1 windows-mcp-proxy`.
- **Tool's args change shape** → same. `template_host` is the source of
  truth.
- **Upstream auth changes** → update `bearer_token` in `config.json`,
  re-call `init`.
- **Fragile installer trees** → don't infer checkbox or expand/collapse state
  from Windows-MCP text alone. Use `proxy_uia_snapshot` for stateful text
  backed by UIA patterns, then `proxy_uia_action` for toggle/expand/collapse
  when patterns are available. Screenshot text remains useful for labels,
  rough coordinates, focused window, and visible buttons.
  UIA scripts are staged through `%TEMP%\windows-mcp-proxy` in chunks to avoid
  Windows command-line length limits.
  If a `SysTreeView32` exposes no UIA `TogglePattern`, use
  `proxy_treeview_snapshot`; it reads `TVIS_STATEIMAGEMASK` via native TreeView
  messages to report checked/unchecked state.
  Use `proxy_treeview_action` for changes. In the HP InstallShield tree tested
  on print05, physical mouse injection from the PowerShell transport failed
  with Win32 error 5 (`Access denied`) and no foreground window, while the
  keyboard path (`TVM_SELECTITEM` + Space) changed and verified check state.
  `click_method="auto"`/`"input"` reports the mouse diagnostics and falls back
  to the verified keyboard path when the state does not change. Raw upstream
  `Click` reported success at the native state-icon coordinate but did not
  change the checkbox state, so verify with `proxy_treeview_snapshot`.

## Things NOT to do

- Don't cache the discovered tool schema to disk "just in case". KISS
  — if the template host is down at `init`, the model gets a clear
  error and the user fixes it.
- Don't add a "ping before each call" step. Stateless-http upstream
  means the ping IS the call; retry-on-error is sufficient.
- Don't switch upstream to `StatefulProxyClient` — would re-introduce
  sticky session state that breaks cross-process safety.
- Don't log to stdout. Ever. That's the MCP transport.
