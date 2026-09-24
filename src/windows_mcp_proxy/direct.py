"""Direct upstream MCP helper for debugging windows-mcp sessions."""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import json
import mimetypes
from pathlib import Path
import re
import sys
from typing import Any

import mcp.types as mcp_types

from windows_mcp_proxy import proxy


def _json_arg(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"invalid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("tool args must be a JSON object")
    return parsed


def _safe_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return safe.strip("-") or "value"


def _extension_for_mime(mime_type: str) -> str:
    if mime_type == "image/png":
        return ".png"
    if mime_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    extension = mimetypes.guess_extension(mime_type)
    return extension or ".bin"


def _load_inventory(config_path: str | None) -> dict[str, Any]:
    if config_path:
        proxy.CONFIG_PATH = Path(config_path).expanduser()
    config = proxy._load_config()
    if config is None:
        raise RuntimeError(f"No inventory found at {proxy.CONFIG_PATH}")
    proxy._config = config
    return config


def _default_host(config: dict[str, Any]) -> str:
    template = config.get("template_host")
    if template:
        return str(template)
    hosts = sorted(config.get("hosts", {}))
    if len(hosts) == 1:
        return hosts[0]
    raise RuntimeError("Pass --host; inventory has no template_host default.")


def _summarize_content(
    content: mcp_types.Content,
    *,
    host: str,
    tool: str,
    index: int,
    out_dir: Path,
) -> dict[str, Any]:
    if isinstance(content, mcp_types.ImageContent):
        raw = base64.b64decode(content.data)
        out_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / (
            f"windows-mcp-{_safe_part(host)}-{_safe_part(tool)}-"
            f"{timestamp}-{index}{_extension_for_mime(content.mime_type)}"
        )
        path.write_bytes(raw)
        return {
            "type": "image",
            "mimeType": content.mime_type,
            "path": str(path),
            "bytes": len(raw),
        }

    if isinstance(content, mcp_types.TextContent):
        return {
            "type": "text",
            "text": content.text,
        }

    dumped = content.model_dump(mode="json", by_alias=True, exclude_none=True)
    data = dumped.get("data")
    if isinstance(data, str):
        dumped["data"] = f"<{len(data)} base64 characters omitted>"
    return dumped


async def _list_tools(host: str, timeout: float | None) -> dict[str, Any]:
    async with proxy._client_for(host, timeout=timeout) as client:
        tools = await client.list_tools()
    return {
        "host": host,
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
            }
            for tool in tools
        ],
    }


async def _call_tool(
    host: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    out_dir: Path,
    timeout: float | None,
) -> dict[str, Any]:
    async with proxy._client_for(host, timeout=timeout) as client:
        result = await client.call_tool_mcp(tool, arguments)

    return {
        "host": host,
        "tool": tool,
        "isError": result.is_error,
        "structuredContent": result.structured_content,
        "content": [
            _summarize_content(
                item,
                host=host,
                tool=tool,
                index=index,
                out_dir=out_dir,
            )
            for index, item in enumerate(result.content, start=1)
        ],
    }


def _timeout(value: float) -> float | None:
    if value <= 0:
        return None
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Call a windows-mcp upstream server directly through the inventory. "
            "Images are saved to disk and bearer tokens are never printed."
        )
    )
    parser.add_argument("tool", nargs="?", help="Upstream tool name to call")
    parser.add_argument("--host", help="Inventory host label; defaults to template_host")
    parser.add_argument(
        "--args",
        type=_json_arg,
        default={},
        help="Tool arguments as a JSON object; default: {}",
    )
    parser.add_argument(
        "--config",
        help="Inventory path; defaults to WINDOWS_MCP_PROXY_CONFIG or .claude/windows-mcp-proxy/config.json",
    )
    parser.add_argument(
        "--out-dir",
        default="/tmp",
        help="Directory for saved image content; default: /tmp",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="HTTP timeout in seconds; set 0 to disable. Default: 30",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List upstream tools for the selected host instead of calling a tool.",
    )
    return parser


async def _amain(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.list_tools and not args.tool:
        parser.error("tool is required unless --list-tools is used")

    try:
        config = _load_inventory(args.config)
        host = args.host or _default_host(config)
        timeout = _timeout(args.timeout)
        if args.list_tools:
            payload = await _list_tools(host, timeout)
        else:
            payload = await _call_tool(
                host,
                args.tool,
                args.args,
                out_dir=Path(args.out_dir).expanduser(),
                timeout=timeout,
            )
    except Exception as e:
        print(json.dumps({"error": str(e)}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
