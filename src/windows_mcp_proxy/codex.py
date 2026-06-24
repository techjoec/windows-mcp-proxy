"""
Codex-compatible eager MCP wrapper.

Codex currently expects a server's tools to be present in the initial tool
list. This entrypoint discovers the upstream windows-mcp schema before stdio
starts, then exposes the same multi-host proxy tools as the lazy server.
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP

from windows_mcp_proxy import proxy


log = logging.getLogger("windows-mcp-proxy.codex")
mcp = FastMCP("windows-mcp-proxy-codex")


async def _configure() -> None:
    ok, count, names = await proxy.configure_eager_or_setup_server(
        mcp,
        server_label="windows-mcp-proxy-codex",
    )
    if ok:
        log.info("codex wrapper ready with %d tools: %s", count, names)


def main() -> None:
    asyncio.run(_configure())
    try:
        mcp.run(show_banner=False)
    except Exception:
        log.exception("fatal in codex mcp.run()")
        raise


if __name__ == "__main__":
    main()
