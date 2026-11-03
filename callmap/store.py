"""Versioned graph snapshots and durable user annotations."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

from callmap.analyzer import Analyzer, digest
from callmap.docs import LibraryDocs
from callmap.scanning import scan_sources

SCHEMA_VERSION = 3
IGNORED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".callmap",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}


class GraphStore:
    def __init__(
        self,
        root: str | Path,
        cache_dir: str | Path | None = None,
        source_roots: list[str] | None = None,
        excludes: list[str] | None = None,
        library_roots: list[str] | None = None,
        jedi: bool = False,
        scan_mode: str = "git",
    ):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"项目目录不存在: {self.root}")
        self.cache = Path(cache_dir).resolve() if cache_dir else self.root / ".callmap"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.database = self.cache / "index.sqlite3"
        self.annotations_path = self.cache / "annotations.json"
        self.source_roots = [
            (self.root / p).resolve() for p in (source_roots or [])
        ] or (
            [self.root / "src", self.root]
            if (self.root / "src").is_dir()
            else [self.root]
        )
        if any(not p.is_relative_to(self.root) for p in self.source_roots):
            raise ValueError("source-root 必须位于项目目录内")
        self.excludes = excludes or []
        self.library_roots = [Path(p).resolve() for p in (library_roots or [])]
        self.jedi = jedi
        if scan_mode not in {"git", "filesystem"}:
            raise ValueError("scan_mode 必须为 git 或 filesystem")
        self.scan_mode = scan_mode
        self._contexts: dict[tuple, dict] = {}
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshot "
                "(id INTEGER PRIMARY KEY, data TEXT)"
            )

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def _manifest(self) -> tuple[list[Path], dict]:
        candidates, self._coverage = scan_sources(
            self.root, self.cache, self.excludes, IGNORED, self.scan_mode
        )
        files, manifest = [], {}
        for path in sorted(set(candidates)):
            rel = path.relative_to(self.root).as_posix()
            try:
                content = path.read_bytes()
            except OSError as exc:
                self._coverage["skipped_files"].append(
                    {"path": rel, "reason": "unreadable", "error": str(exc)}
                )
                self._coverage["audit_complete"] = False
                continue
            files.append(path)
            manifest[rel] = hashlib.sha256(content).hexdigest()
        self._coverage["indexed_files"] = [
            p.relative_to(self.root).as_posix() for p in files
        ]
        self._coverage["effective_config"] = {
            "scan_mode": self.scan_mode,
            "project_root": str(self.root),
            "source_roots": [str(p) for p in self.source_roots],
            "source_root_role": "module_name_resolution",
            "excludes": self.excludes,
            "builtin_excludes": sorted(IGNORED),
            "library_roots": [str(p) for p in self.library_roots],
            "jedi": self.jedi,
        }
        # Environment/config changes invalidate resolution, too.
        for name in ("pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt"):
            path = self.root / name
            if path.is_file():
                manifest["config:" + name] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
        return files, manifest

    def ensure(self, force: bool = False) -> dict:
        with self.lock:
            files, manifest = self._manifest()
            options = {
                "schema": SCHEMA_VERSION,
                "root": str(self.root),
                "sources": [str(p) for p in self.source_roots],
                "excludes": self.excludes,
                "jedi": self.jedi,
                "scan_mode": self.scan_mode,
                "libraries": [str(p) for p in self.library_roots],
                "python": sys.version,
                "executable": sys.executable,
                "engine": digest(
                    "".join(
                        (Path(__file__).parent / name).read_text()
                        for name in (
                            "analyzer.py",
                            "docs.py",
                            "store.py",
                            "flow.py",
                            "stdlib_models.py",
                            "registry.py",
                            "scanning.py",
                        )
                    )
                ),
                "packages": sorted(
                    [dist.metadata.get("Name", ""), dist.version]
                    for dist in importlib.metadata.distributions()
                ),
            }
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT data FROM snapshot WHERE id=1"
                ).fetchone()
            previous = json.loads(row[0]) if row else None
            libraries_unchanged = previous is not None and all(
                Path(p).is_file()
                and hashlib.sha256(Path(p).read_bytes()).hexdigest() == h
                for p, h in previous.get("library_manifest", {}).items()
            )
            if (
                not force
                and previous
                and libraries_unchanged
                and previous["manifest"] == manifest
                and previous["options"] == options
            ):
                previous["cache_hit"] = True
                previous["coverage"] = self._coverage
                return previous
            started = time.monotonic()
            docs = LibraryDocs(self.library_roots)
            graph = Analyzer(
                self.root, files, self.source_roots, self.jedi, docs
            ).analyze()
            for node in graph["nodes"]:
                if node["kind"] == "external" and node["doc_source"] == "missing":
                    node.update(docs.lookup(node["qualified_name"]))
            library_manifest = {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in docs.cache
                if p.is_file()
            }
            referenced = {e["target"] for e in graph["edges"]}
            graph["nodes"] = [
                n
                for n in graph["nodes"]
                if n["kind"] == "internal" or n["id"] in referenced
            ]
            graph.update(
                {
                    "root": str(self.root),
                    "manifest": manifest,
                    "options": options,
                    "library_manifest": library_manifest,
                    "snapshot": digest(
                        json.dumps(
                            [manifest, options, library_manifest], sort_keys=True
                        )
                    ),
                    "indexed_at": time.time(),
                    "build_seconds": round(time.monotonic() - started, 3),
                    "file_count": len(files),
                    "coverage": self._coverage,
                    "cache_hit": False,
                    "update_strategy": "unchanged: reuse; changed: full rebuild",
                }
            )
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO snapshot VALUES (1, ?)",
                    (json.dumps(graph, ensure_ascii=False),),
                )
            return graph

    def annotations(self) -> dict:
        if not self.annotations_path.exists():
            return {}
        return json.loads(self.annotations_path.read_text())

    def graph(self, context_entry: str | None = None) -> dict:
        graph = self.ensure()
        if context_entry is not None:
            entry = self.select(graph, context_entry)
            if entry["kind"] != "internal":
                raise ValueError("context_entry 必须是项目内部入口")
            key = (graph["snapshot"], entry["identity"])
            with self.lock:
                if key not in self._contexts:
                    files = [self.root / p for p in graph["coverage"]["indexed_files"]]
                    contextual = Analyzer(
                        self.root,
                        files,
                        self.source_roots,
                        self.jedi,
                        LibraryDocs(self.library_roots),
                    ).analyze(entry["identity"])
                    self._contexts[key] = contextual
                    if len(self._contexts) > 8:
                        del self._contexts[next(iter(self._contexts))]
                graph.update(copy.deepcopy(self._contexts[key]))
            graph["context"] = {
                "entry": entry["id"],
                "mode": "reachable_callers",
                "runtime_complete": False,
                "note": "按入口可达的静态调用传播参数；"
                "不保证覆盖运行时插件、注册加载和未知输入。",
            }
        nodes = {n["id"]: n for n in graph["nodes"]}
        annotations = self.annotations()
        graph["annotation_warnings"] = []
        replacements = {}
        for sid, annotation in annotations.items():
            node = nodes.get(sid)
            if node is None:
                graph["annotation_warnings"].append(f"补充信息的节点已不存在: {sid}")
                continue
            stale = (
                annotation["hash"] != node["hash"]
                or any(
                    tid not in nodes or nodes[tid]["hash"] != value
                    for tid, value in annotation.get("target_hashes", {}).items()
                )
                or annotation.get("environment") != graph["snapshot"]
                and node["kind"] in {"external", "unresolved"}
            )
            node["annotation"] = annotation | {"stale": stale}
            if stale:
                graph["annotation_warnings"].append(
                    f"人工补充需要复核: {node['qualified_name']}"
                )
            elif annotation.get("targets"):
                replacements[sid] = annotation["targets"]
            if annotation.get("doc"):
                node["automatic_doc"] = node["doc"]
                node["doc"] = annotation["doc"]
                node["doc_source"] = "manual-stale" if stale else "manual"
        edges = []
        for edge in graph["edges"]:
            if edge["target"] in replacements:
                for target in replacements[edge["target"]]:
                    edges.append(
                        edge
                        | {
                            "id": edge["id"] + ":" + target,
                            "target": target,
                            "status": "manual",
                            "reason": "manual",
                            "original_target": edge["target"],
                        }
                    )
            else:
                edges.append(edge)
        graph["edges"] = edges
        return graph

    def annotate(self, symbol: str, doc: str, targets: list[str] | None = None) -> dict:
        with self.lock:
            graph = self.ensure()
            node = self.select(graph, symbol)
            target_nodes = [self.select(graph, t) for t in (targets or [])]
            if target_nodes and node["kind"] != "unresolved":
                raise ValueError("只有待确认节点可以补充调用目标")
            if any(n["kind"] == "unresolved" for n in target_nodes):
                raise ValueError("调用目标必须是内部函数、类或外部接口")
            annotation = {
                "doc": doc,
                "hash": node["hash"],
                "updated_at": time.time(),
                "environment": graph["snapshot"],
                "targets": [n["id"] for n in target_nodes],
                "target_hashes": {n["id"]: n["hash"] for n in target_nodes},
            }
            # Serialize separate CLI/MCP/UI writers through the shared database.
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                annotations = self.annotations()
                annotations[node["id"]] = annotation
                temporary = self.annotations_path.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_text(
                    json.dumps(annotations, ensure_ascii=False, indent=2)
                )
                temporary.replace(self.annotations_path)
            return annotation

    @staticmethod
    def select(graph: dict, symbol: str) -> dict:
        nodes = graph["nodes"]
        matches = [
            n for n in nodes if symbol in {n["id"], n["identity"], n["qualified_name"]}
        ]
        if not matches:
            matches = [
                n
                for n in nodes
                if n["kind"] == "internal"
                and (n["name"] == symbol or n["qualified_name"].endswith("." + symbol))
            ]
        if len(matches) != 1:
            choices = [n["identity"] for n in matches[:20]]
            raise ValueError(f"入口未找到或不唯一: {symbol}; 候选: {choices}")
        return matches[0]

    def search(self, query: str, limit: int = 50) -> list[dict]:
        graph = self.graph()
        return [
            {
                k: n[k]
                for k in (
                    "id",
                    "identity",
                    "qualified_name",
                    "kind",
                    "path",
                    "line",
                    "signature",
                )
            }
            for n in graph["nodes"]
            if query.lower() in n["qualified_name"].lower()
        ][:limit]

    def expand(
        self,
        symbol: str,
        depth: int = -1,
        max_nodes: int = 500,
        include_code: bool = False,
        *,
        context_entry: str | None = None,
        prefer_internal: bool = False,
        internal_only: bool = False,
    ) -> dict:
        if depth < -1 or max_nodes < 1:
            raise ValueError("depth 必须 >= -1；max_nodes 必须 >= 1")
        graph = self.graph(context_entry)
        root = self.select(graph, symbol)
        by_id = {n["id"]: n for n in graph["nodes"]}
        outgoing = defaultdict_list(graph["edges"], "source")
        if internal_only:
            outgoing = {
                sid: [e for e in edges if by_id[e["target"]]["kind"] != "external"]
                for sid, edges in outgoing.items()
            }
        if prefer_internal:
            outgoing = {
                sid: sorted(
                    edges, key=lambda e: by_id[e["target"]]["kind"] != "internal"
                )
                for sid, edges in outgoing.items()
            }
        visited = {root["id"]: 0}
        queue = deque([root["id"]])
        omitted = set()
        deferred = {}
        while queue:
            sid = queue.popleft()
            for edge in outgoing.get(sid, []):
                target = edge["target"]
                if target in visited:
                    continue
                if prefer_internal and by_id[target]["kind"] == "external":
                    if depth >= 0 and visited[sid] >= depth:
                        omitted.add(target)
                    else:
                        distance = visited[sid] + 1
                        deferred[target] = min(distance, deferred.get(target, distance))
                    continue
                if (depth >= 0 and visited[sid] >= depth) or len(visited) >= max_nodes:
                    omitted.add(target)
                    continue
                visited[target] = visited[sid] + 1
                queue.append(target)
        for target, distance in sorted(deferred.items(), key=lambda item: item[1]):
            if len(visited) < max_nodes:
                visited[target] = distance
            else:
                omitted.add(target)
        # Include all edges between visible nodes, including cycles.
        edges = [
            e
            for e in graph["edges"]
            if e["source"] in visited
            and e["target"] in visited
            and (not internal_only or by_id[e["target"]]["kind"] != "external")
        ]
        nodes = []
        for sid, distance in visited.items():
            node = by_id[sid].copy()
            if not include_code:
                node.pop("code", None)
            nodes.append(
                node | {"depth": distance, "outgoing_count": len(outgoing.get(sid, []))}
            )
        return {
            "entry": root["id"],
            "project_path": str(self.root),
            "nodes": nodes,
            "edges": edges,
            "snapshot": graph["snapshot"],
            "cache_hit": graph["cache_hit"],
            "truncated": bool(omitted - visited.keys()),
            "graph_truncated": bool(omitted - visited.keys()),
            "external_nodes_hidden": internal_only,
            "context": graph.get("context"),
            "context_entry_reaches_symbol": context_entry is None
            or root["id"] in graph.get("context_scope", []),
            "omitted_frontier_count": len(omitted - visited.keys()),
            "omitted_frontier": [
                {k: by_id[sid][k] for k in ("id", "qualified_name", "path", "line")}
                for sid in sorted(omitted - visited.keys())[:5]
            ],
            "hidden_external_node_count": len(
                {
                    e["target"]
                    for e in graph["edges"]
                    if internal_only
                    and e["source"] in visited
                    and by_id[e["target"]]["kind"] == "external"
                }
            ),
            "errors": graph["errors"],
            "annotation_warnings": graph["annotation_warnings"],
            "analysis": graph.get("analysis", {}),
            "inference_incomplete": not graph.get("analysis", {}).get(
                "argument_converged", True
            )
            or bool(graph.get("analysis", {}).get("saturated_parameters", 0))
            or bool(graph.get("analysis", {}).get("saturated_returns", 0)),
        }

    def context(
        self,
        symbol: str,
        budget: int = 12000,
        depth: int = -1,
        include_code: bool = False,
        *,
        context_entry: str | None = None,
        include_external_docs: bool = False,
    ) -> str:
        if budget < 256:
            raise ValueError("字符预算至少为 256")
        graph = self.expand(
            symbol,
            depth=depth,
            include_code=include_code,
            context_entry=context_entry,
            prefer_internal=True,
        )
        lines = [
            f"# Callmap: {symbol}",
            f"Snapshot: {graph['snapshot']}",
            "静态可能调用关系；candidate 为候选，unresolved 为待确认。",
            f"图裁剪: {graph['truncated']}；解析错误: {len(graph['errors'])}；"
            f"补充复核: {len(graph['annotation_warnings'])}",
            f"推断达到上限: {graph['inference_incomplete']}；"
            "候选边不代表运行时必然调用。",
        ]
        outgoing = defaultdict_list(graph["edges"], "source")
        used = len("\n".join(lines))
        for node in graph["nodes"]:
            block = [
                f"\n## {node['qualified_name']} [{node['id']}] ({node['kind']})",
                f"{node['path']}:{node['line']} | {node['signature']}",
            ]
            block += [
                f"- {e['expression']} @L{e['line']} -> {e['target']} "
                f"[{e['status']}; {e['reason']}]"
                + (
                    f" 依据: {e['evidence'][0].get('path', '')}:"
                    f"{e['evidence'][0].get('line', '')}"
                    if e.get("evidence")
                    else ""
                )
                for e in outgoing.get(node["id"], [])
            ]
            if node["doc"] and (node["kind"] != "external" or include_external_docs):
                block.append(f"说明 ({node['doc_source']}): {node['doc']}")
            if include_code and node.get("code"):
                block.append("```python\n" + node["code"] + "\n```")
            text = "\n".join(block)
            if used + len(text) + 100 > budget:
                remaining = budget - used - 100
                if remaining > 0:
                    # Do not cut a source block in half.
                    header = "\n".join(block[:2])
                    if len(header) <= remaining:
                        lines.append(header)
                lines.append(
                    "\n[字符预算已达上限；"
                    "未输出的节点/源码请用 expand 或 node 按需读取。]"
                )
                break
            lines.append(text)
            used += len(text) + 1
        return "\n".join(lines)

    def stats(
        self, force: bool = False, file_offset: int = 0, file_limit: int = 100
    ) -> dict:
        if file_offset < 0 or not 1 <= file_limit <= 2000:
            raise ValueError("file_offset 必须 >= 0；file_limit 必须为 1..2000")
        graph = self.ensure(force)
        coverage = graph["coverage"].copy()
        totals = {}
        for field in ("indexed_files", "skipped_files", "skipped_directories"):
            totals[field] = len(coverage[field])
            coverage[field] = coverage[field][file_offset : file_offset + file_limit]
        coverage["totals"] = totals
        coverage["file_offset"] = file_offset
        coverage["next_offset"] = (
            file_offset + file_limit
            if max(totals.values()) > file_offset + file_limit
            else None
        )
        coverage["parse_errors_in_indexed_scope"] = len(graph["errors"])
        return {
            "root": graph["root"],
            "files": graph["file_count"],
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "callsites": len(
                {
                    (e["source"], e["line"], e["column"], e["expression"])
                    for e in graph["edges"]
                    if not e.get("synthetic", e["reason"] == "constructor")
                }
            ),
            "node_kinds": dict(Counter(n["kind"] for n in graph["nodes"])),
            "edge_status": dict(Counter(e["status"] for e in graph["edges"])),
            "errors": graph["errors"],
            "snapshot": graph["snapshot"],
            "cache_hit": graph["cache_hit"],
            "build_seconds": graph["build_seconds"],
            "analysis": graph.get("analysis", {}),
            "cache_dir": str(self.cache),
            "coverage": coverage,
        }


def defaultdict_list(items: list[dict], key: str) -> dict[str, list]:
    result: dict[str, list] = {}
    for item in items:
        result.setdefault(item[key], []).append(item)
    return result
