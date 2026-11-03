"""Check user registrations and exercise one server against multiple projects."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tomllib
from datetime import timedelta
from pathlib import Path

from callmap.registration import CLIENTS, config_path, read_json_config


async def verify(cache_root: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    commands = {}
    for client in CLIENTS:
        path = config_path(client)
        if client == "codex":
            config = tomllib.loads(path.read_text())["mcp_servers"]["callmap"]
            commands[client] = [config["command"], *config["args"]]
        elif client == "claude":
            config = read_json_config(path)["mcpServers"]["callmap"]
            commands[client] = [config["command"], *config["args"]]
        else:
            config = read_json_config(path)["mcp"]["callmap"]
            commands[client] = config["command"]
        if "--project" in commands[client] or commands[client][-1] != "mcp":
            raise ValueError(f"{client} 未注册全局多项目服务")
    if any(command != commands["codex"] for command in commands.values()):
        raise ValueError("三个客户端的 Callmap 启动命令不一致")
    command = commands["codex"]
    # Keep this verification's caches in the supplied writable artifact directory.
    environment = dict(os.environ)
    environment["XDG_CACHE_HOME"] = str(cache_root.resolve())
    params = StdioServerParameters(
        command=command[0], args=command[1:], env=environment, cwd="/private/tmp"
    )
    report = {"registrations": {c: "verified" for c in CLIENTS}, "projects": []}
    root = Path(__file__).resolve().parents[1]
    async with stdio_client(params) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=180)
        ) as session:
            await session.initialize()
            report["tools"] = [tool.name for tool in (await session.list_tools()).tools]
            for project, symbol in (
                (root.parent / "flux_test_be", "RunnerFactory.get_runner"),
                (root, "callmap.cli.main"),
            ):
                args = {"project_path": str(project)}
                result = await session.call_tool("index_status", args)
                if result.isError:
                    raise ValueError(result.content[0].text)
                stats = json.loads(result.content[0].text)
                if stats["root"] != str(project) or stats["errors"]:
                    raise ValueError("索引项目不匹配或存在解析错误")
                result = await session.call_tool(
                    "read_symbol", args | {"symbol": symbol}
                )
                if result.isError:
                    raise ValueError(result.content[0].text)
                node = json.loads(result.content[0].text)
                if not node["code"]:
                    raise ValueError("未返回目标源码")
                result = await session.call_tool("index_status", args)
                cached = json.loads(result.content[0].text)
                if not cached["cache_hit"]:
                    raise ValueError("未复用缓存")
                report["projects"].append(
                    {
                        "root": stats["root"],
                        "files": stats["files"],
                        "cache_dir": stats["cache_dir"],
                        "cache_hit": cached["cache_hit"],
                        "symbol": node["qualified_name"],
                    }
                )
    if report["projects"][0]["cache_dir"] == report["projects"][1]["cache_dir"]:
        raise ValueError("项目缓存未隔离")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=Path("artifacts/global-mcp"))
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/global-mcp-report.json")
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(verify(args.cache_root))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
