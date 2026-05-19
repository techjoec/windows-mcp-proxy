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

In `<project>/.mcp.json`:

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

## First run

On first invocation only one tool is exposed: `init`. Calling it with no
inventory file returns the JSON shape you need to create. Write the file,
call `init` again, and the proxy:

1. Connects to `template_host` and lists its tools.
2. Re-exposes each tool with a required `host` argument (enum of your labels).
3. Sends `notifications/tools/list_changed` so the client refreshes.

### Inventory file

Location: `$PWD/.claude/windows-mcp-proxy/config.json` (per-project).

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
subprocess with its own httpx pool. Because upstream is stateless-http,
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
