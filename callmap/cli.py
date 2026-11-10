"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from callmap.store import GraphStore


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Python 调用图：持久索引、循环去重、源码与接口说明"
    )
    result.add_argument("--project", help="待分析项目目录；全局 MCP 默认无绑定项目")
    result.add_argument(
        "--cache-dir", help="索引目录；无 --project 的 MCP 模式下为多项目缓存根"
    )
    result.add_argument("--source-root", action="append", help="相对项目路径，可重复")
    result.add_argument(
        "--library-root", action="append", help="外部依赖源码/site-packages 目录"
    )
    result.add_argument("--exclude", action="append", help="排除 glob，例如 tests/**")
    result.add_argument(
        "--jedi", action="store_true", help="对未解析调用尝试 Jedi（更慢）"
    )
    result.add_argument(
        "--scan-mode",
        choices=["git", "filesystem"],
        default="git",
        help="git 遵守 Git 文件列表；filesystem 纳入嵌套仓库和 Git 忽略的 Python 文件",
    )
    commands = result.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="创建或更新索引")
    index.add_argument("--force", action="store_true")
    search = commands.add_parser("search", help="查找入口函数")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    for flag in ("symbol-type", "path", "decorator", "route", "owner", "entry-kind"):
        search.add_argument("--" + flag)
    refs = commands.add_parser("references", help="查询字段/变量定义与读写位置")
    refs.add_argument("query")
    refs.add_argument("--path")
    refs.add_argument("--kind", choices=["definition", "read", "write"])
    refs.add_argument("--limit", type=int, default=50)
    refs.add_argument("--offset", type=int, default=0)
    impact = commands.add_parser("impact", help="关联 Git 变更、静态入口与测试调用")
    impact.add_argument("--base", default="HEAD")
    impact.add_argument("--patch-file", type=Path)
    impact.add_argument("--limit", type=int, default=20)
    impact.add_argument("--offset", type=int, default=0)
    impact.add_argument("--max-nodes", type=int, default=200)
    entries = commands.add_parser("entries", help="自动发现明确入口和候选入口")
    entries.add_argument("--limit", type=int, default=50)
    entries.add_argument("--offset", type=int, default=0)
    for command in ("expand", "callers", "context", "node"):
        child = commands.add_parser(
            command,
            **(
                {"help": "查询上游调用者；默认直接上游"} if command == "callers" else {}
            ),
        )
        child.add_argument("symbol", help="函数限定名、文件.py::函数名或节点 ID")
        child.add_argument("--output", help="保存到文件")
        if command != "node":
            child.add_argument(
                "--depth",
                type=int,
                default=1 if command == "callers" else -1,
                help="-1 表示全部可达节点；callers 默认只查直接上游",
            )
            child.add_argument("--code", action="store_true", help="包含完整源码")
        if command in {"expand", "callers"}:
            child.add_argument("--max-nodes", type=int, default=500)
            child.add_argument("--format", choices=["json", "mermaid"], default="json")
            child.add_argument(
                "--compact", action="store_true", help="精简 JSON；默认字符预算 20000"
            )
            child.add_argument(
                "--budget", type=int, help="JSON 响应字符预算（至少 2000）"
            )
            child.add_argument("--include-docs", action="store_true")
            child.add_argument("--prefer-internal", action="store_true")
            child.add_argument("--internal-only", action="store_true")
            child.add_argument("--cursor", type=int, default=0)
            child.add_argument("--snapshot")
        if command == "context":
            child.add_argument(
                "--direction", choices=["downstream", "upstream"], default="downstream"
            )
            child.add_argument(
                "--budget", type=int, default=12000, help="字符预算（不是 token）"
            )
            child.add_argument("--include-external-docs", action="store_true")
        if command in {"expand", "callers", "context"}:
            child.add_argument(
                "--context-entry", help="按指定内部入口可达的调用传播参数"
            )
    annotate = commands.add_parser("annotate", help="补充说明或待确认调用目标")
    annotate.add_argument("symbol")
    annotate.add_argument("--doc", default="")
    annotate.add_argument("--target", action="append")
    serve = commands.add_parser("serve", help="启动本地可视化")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("mcp", help="启动 MCP stdio 服务")
    register = commands.add_parser("register", help="生成或应用用户级 MCP 注册")
    register.add_argument(
        "--client",
        action="append",
        choices=["all", "codex", "claude", "opencode"],
        help="可重复；默认 all（已安装的客户端）",
    )
    register.add_argument("--apply", action="store_true", help="写入客户端配置")
    register.add_argument("--replace", action="store_true", help="替换已有同名配置")
    register.add_argument("--name", default="callmap")
    return result


def mermaid(graph: dict) -> str:
    lines = ["flowchart LR"]
    for node in graph["nodes"]:
        label = node["qualified_name"].replace('"', "'").replace("<", "&lt;")
        lines.append(f'  {node["id"]}["{label}"]')
    seen = set()
    for edge in graph["edges"]:
        key = (edge["source"], edge["target"], edge["status"])
        if key in seen:
            continue
        seen.add(key)
        arrow = "-->" if edge["status"] in {"resolved", "manual"} else "-.->"
        lines.append(f"  {edge['source']} {arrow} {edge['target']}")
    return "\n".join(lines)


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command == "register":
            from callmap.registration import register_clients

            if args.project:
                raise ValueError(
                    "全局注册不绑定 --project；由工具调用指定 project_path"
                )
            output = register_clients(
                args.client,
                args.name,
                args.cache_dir,
                args.apply,
                args.replace,
                source_roots=args.source_root,
                excludes=args.exclude,
                library_roots=args.library_root,
                jedi=args.jedi,
                scan_mode=args.scan_mode,
            )
            print(json.dumps(output, ensure_ascii=False, indent=2))
            if args.apply and any(r["status"] == "error" for r in output["clients"]):
                raise SystemExit(1)
            return
        if args.command == "mcp":
            from callmap.mcp_server import run
            from callmap.projects import ProjectStores

            default_store = (
                GraphStore(
                    args.project,
                    args.cache_dir,
                    args.source_root,
                    args.exclude,
                    args.library_root,
                    args.jedi,
                    args.scan_mode,
                )
                if args.project
                else None
            )
            run(
                ProjectStores(
                    default_store,
                    args.cache_dir if default_store is None else None,
                    args.source_root,
                    args.exclude,
                    args.library_root,
                    args.jedi,
                    args.scan_mode,
                )
            )
            return
        store = GraphStore(
            args.project or ".",
            args.cache_dir,
            args.source_root,
            args.exclude,
            args.library_root,
            args.jedi,
            args.scan_mode,
        )
        if args.command == "serve":
            from callmap.server import serve

            serve(store, args.port)
            return
        if args.command == "index":
            output = store.stats(args.force)
        elif args.command == "search":
            output = store.search(
                args.query,
                args.limit,
                symbol_type=args.symbol_type,
                path=args.path,
                decorator=args.decorator,
                route=args.route,
                owner=args.owner,
                entry_kind=args.entry_kind,
            )
        elif args.command == "references":
            output = store.references(
                args.query,
                path=args.path,
                kind=args.kind,
                limit=args.limit,
                offset=args.offset,
            )
        elif args.command == "impact":
            from callmap.impact import impact

            output = impact(
                store,
                args.base,
                args.patch_file.read_text() if args.patch_file else None,
                args.limit,
                args.offset,
                args.max_nodes,
            )
        elif args.command == "entries":
            output = store.entries(args.limit, args.offset)
        elif args.command == "node":
            output = store.read_node(args.symbol)
        elif args.command in {"expand", "callers"}:
            query_graph = store.callers if args.command == "callers" else store.expand
            output = query_graph(
                args.symbol,
                args.depth,
                args.max_nodes,
                args.code,
                context_entry=args.context_entry,
                prefer_internal=args.prefer_internal,
                internal_only=args.internal_only,
            )
            if args.format == "mermaid":
                output = mermaid(output)
            elif args.compact or args.budget is not None:
                from callmap.views import bounded_graph

                output = bounded_graph(
                    output,
                    {
                        "symbol": args.symbol,
                        "project_path": str(store.root),
                        "depth": args.depth,
                        "max_nodes": args.max_nodes,
                        "context_entry": args.context_entry,
                        "compact": args.compact,
                        "include_docs": args.include_docs,
                        "prefer_internal": args.prefer_internal,
                        "internal_only": args.internal_only,
                        "excludes": args.exclude,
                        "scan_mode": args.scan_mode,
                        "budget": args.budget if args.budget is not None else 20000,
                    },
                    budget=args.budget if args.budget is not None else 20000,
                    compact=args.compact,
                    include_docs=args.include_docs,
                    cursor=args.cursor,
                    snapshot=args.snapshot,
                )
        elif args.command == "context":
            output = store.context(
                args.symbol,
                args.budget,
                args.depth,
                args.code,
                context_entry=args.context_entry,
                include_external_docs=args.include_external_docs,
                direction=args.direction,
            )
        else:
            output = store.annotate(args.symbol, args.doc, args.target)
        text = (
            output
            if isinstance(output, str)
            else json.dumps(output, ensure_ascii=False, indent=2)
        )
        if getattr(args, "output", None):
            Path(args.output).write_text(text + "\n")
        else:
            print(text)
    except (ValueError, OSError, ImportError) as exc:
        print(f"callmap: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
