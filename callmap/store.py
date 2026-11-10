"""Versioned graph snapshots and durable user annotations."""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import sys
import threading
import time
import weakref
from collections import Counter, deque
from pathlib import Path

from callmap.analyzer import Analyzer, digest
from callmap.cache import FileHashes, GraphIndexes, file_stamp
from callmap.docs import LibraryDocs
from callmap.entrypoints import discover_entry_points
from callmap.query_report import report
from callmap.references import search_references
from callmap.scanning import scan_sources
from callmap.semantics import describe_edges

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
        self._hashes = FileHashes()
        self._snapshot: dict | None = None
        self._snapshot_stamp = None
        self._snapshot_generation = 0
        self._view_key = None
        self._view = None
        self._annotation_cache = None
        self._base_graph = None
        self._base_indexes = None
        self._database_reader = None
        self._reader_identity = None
        self._reader_finalizer = None
        self.lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS snapshot "
                "(id INTEGER PRIMARY KEY, data TEXT)"
            )

    def _connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def _manifest(self, force: bool = False) -> tuple[list[Path], dict]:
        candidates, self._coverage = scan_sources(
            self.root, self.cache, self.excludes, IGNORED, self.scan_mode
        )
        files, manifest = [], {}
        for path in sorted(set(candidates)):
            rel = path.relative_to(self.root).as_posix()
            try:
                value = self._hashes.digest(path, force)
            except OSError as exc:
                self._coverage["skipped_files"].append(
                    {"path": rel, "reason": "unreadable", "error": str(exc)}
                )
                self._coverage["audit_complete"] = False
                continue
            files.append(path)
            manifest[rel] = value
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
                manifest["config:" + name] = self._hashes.digest(path, force)
        return files, manifest

    def ensure(self, force: bool = False) -> dict:
        """Return an isolated full snapshot; query APIs copy only their results."""
        return copy.deepcopy(self._ensure_snapshot(force))

    def _database_stamp(self):
        stamps = []
        for path in (self.database, Path(str(self.database) + "-wal")):
            try:
                stamps.append(file_stamp(path))
            except FileNotFoundError:
                stamps.append(None)
        identity = stamps[0][:2] if stamps[0] is not None else None
        if self._database_reader is None or identity != self._reader_identity:
            if self._reader_finalizer is not None:
                self._reader_finalizer()
            self._database_reader = sqlite3.connect(
                self.database, timeout=30, check_same_thread=False
            )
            self._reader_identity = identity
            self._reader_finalizer = weakref.finalize(self, self._database_reader.close)
        # data_version belongs to this long-lived connection. It catches commits
        # even when WAL bytes/stat precede publication of the committed read view.
        version = self._database_reader.execute("PRAGMA data_version").fetchone()[0]
        return tuple(stamps), version

    def _publish_snapshot(self, graph, stamp) -> None:
        self._snapshot = graph
        self._snapshot_stamp = stamp
        self._snapshot_generation += 1
        self._base_graph = None
        self._base_indexes = None
        self._contexts.clear()
        self._view = None
        self._view_key = None

    def _ensure_snapshot(self, force: bool = False) -> dict:
        with self.lock:
            files, manifest = self._manifest(force)
            options = {
                "schema": SCHEMA_VERSION,
                "root": str(self.root),
                "sources": [str(p) for p in self.source_roots],
                "excludes": list(self.excludes),
                "jedi": self.jedi,
                "scan_mode": self.scan_mode,
                "libraries": [str(p) for p in self.library_roots],
                "python": sys.version,
                "executable": sys.executable,
                "engine": digest(
                    "".join(
                        self._hashes.digest(Path(__file__).parent / name, force)
                        for name in (
                            "analyzer.py",
                            "docs.py",
                            "store.py",
                            "flow.py",
                            "stdlib_models.py",
                            "registry.py",
                            "scanning.py",
                            "entrypoints.py",
                            "semantics.py",
                            "query_report.py",
                            "cache.py",
                            "provenance.py",
                            "references.py",
                            "diagnostics.py",
                        )
                    )
                ),
                "packages": sorted(
                    [dist.metadata.get("Name", ""), dist.version]
                    for dist in importlib.metadata.distributions()
                ),
            }
            stamp = self._database_stamp()
            previous = self._snapshot
            if previous is None or stamp != self._snapshot_stamp:
                with self._connect() as connection:
                    row = connection.execute(
                        "SELECT data FROM snapshot WHERE id=1"
                    ).fetchone()
                previous = json.loads(row[0]) if row else None
                self._publish_snapshot(
                    previous, stamp if stamp == self._database_stamp() else None
                )
            libraries_unchanged = previous is not None and all(
                Path(p).is_file() and self._hashes.digest(Path(p), force) == h
                for p, h in previous.get("library_manifest", {}).items()
            )
            if (
                not force
                and previous
                and libraries_unchanged
                and previous["manifest"] == manifest
                and previous["options"] == options
            ):
                return previous | {"cache_hit": True, "coverage": self._coverage}
            started = time.monotonic()
            docs = LibraryDocs(self.library_roots)
            graph = Analyzer(
                self.root, files, self.source_roots, self.jedi, docs
            ).analyze()
            for node in graph["nodes"]:
                if node["kind"] == "external" and node["doc_source"] == "missing":
                    node.update(docs.lookup(node["qualified_name"]))
            library_manifest = {
                str(p): self._hashes.digest(p, force) for p in docs.cache if p.is_file()
            }
            referenced = {e["target"] for e in graph["edges"]}
            graph["nodes"] = [
                n
                for n in graph["nodes"]
                if n["kind"] == "internal" or n["id"] in referenced
            ]
            graph["entry_points"] = discover_entry_points(self.root, graph)
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
            self._publish_snapshot(graph, self._database_stamp())
            return graph

    def annotations(self) -> dict:
        with self.lock:
            return copy.deepcopy(self._read_annotations()[1])

    def _read_annotations(self) -> tuple[str, dict]:
        try:
            before = file_stamp(self.annotations_path)
        except FileNotFoundError:
            self._annotation_cache = None
            return "", {}
        if self._annotation_cache and self._annotation_cache[0] == before:
            return self._annotation_cache[1:]
        content = self.annotations_path.read_bytes()
        value = hashlib.sha256(content).hexdigest()
        data = json.loads(content)
        if before == file_stamp(self.annotations_path):
            self._annotation_cache = (before, value, data)
        else:
            self._annotation_cache = None
        return value, data

    def graph(self, context_entry: str | None = None) -> dict:
        graph = self._query_graph(context_entry)
        return copy.deepcopy({k: v for k, v in graph.items() if not k.startswith("_")})

    def read_node(self, symbol: str, context_entry: str | None = None) -> dict:
        graph = self._query_graph(context_entry)
        return copy.deepcopy(self.select(graph, symbol)) | {
            "snapshot": graph["snapshot"],
            "analysis_fingerprint": graph["analysis_fingerprint"],
        }

    def _query_graph(self, context_entry: str | None = None) -> dict:
        with self.lock:
            return self._query_graph_locked(context_entry)

    def _query_graph_locked(self, context_entry: str | None = None) -> dict:
        graph = dict(self._ensure_snapshot())
        if self._base_graph is not self._snapshot:
            self._base_graph = self._snapshot
            self._base_indexes = GraphIndexes(graph)
        graph["_indexes"] = self._base_indexes
        annotation_key, annotations = self._read_annotations()
        graph["analysis_fingerprint"] = digest(
            json.dumps([graph["snapshot"], annotation_key])
        )
        view_key = (self._snapshot_generation, context_entry, annotation_key)
        if self._view_key == view_key and self._view is not None:
            return self._view | {
                "cache_hit": graph["cache_hit"],
                "coverage": graph["coverage"],
            }
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
                graph.update(self._contexts[key])
            graph["context"] = {
                "entry": entry["id"],
                "mode": "reachable_callers",
                "runtime_complete": False,
                "note": "按入口可达的静态调用传播参数；"
                "不保证覆盖运行时插件、注册加载和未知输入。",
            }
        nodes = {n["id"]: n for n in graph["nodes"]}
        graph["nodes"] = list(graph["nodes"])
        positions = {node["id"]: i for i, node in enumerate(graph["nodes"])}
        graph["annotation_warnings"] = []
        replacements = {}
        for sid, annotation in annotations.items():
            node = nodes.get(sid)
            if node is None:
                graph["annotation_warnings"].append(f"补充信息的节点已不存在: {sid}")
                continue
            node = node.copy()
            nodes[sid] = node
            graph["nodes"][positions[sid]] = node
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
        affected_sites = {
            (e["source"], e["line"], e["column"], e["expression"])
            for e in graph["edges"]
            if e["target"] in replacements
        }
        affected_edges = []
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
                            "evidence": edge["evidence"]
                            + [
                                {
                                    "kind": "manual-annotation",
                                    "original_target": edge["target"],
                                    "target_id": target,
                                }
                            ],
                        }
                    )
            else:
                if (
                    edge["source"],
                    edge["line"],
                    edge["column"],
                    edge["expression"],
                ) in affected_sites:
                    edge = edge.copy()
                edges.append(edge)
        graph["edges"] = edges
        if replacements:
            affected_edges = [
                e
                for e in edges
                if (e["source"], e["line"], e["column"], e["expression"])
                in affected_sites
            ]
            describe_edges(affected_edges, nodes)
        graph["_indexes"] = (
            self._base_indexes
            if context_entry is None and not annotations
            else GraphIndexes(graph)
        )
        self._view_key = view_key
        self._view = graph
        return graph

    def annotate(
        self,
        symbol: str,
        doc: str,
        targets: list[str] | None = None,
        *,
        expected_fingerprint=None,
        context_entry=None,
    ) -> dict:
        with self.lock:
            graph = (
                self._query_graph(context_entry)
                if expected_fingerprint
                else self._ensure_snapshot()
            )
            if (
                expected_fingerprint
                and graph["analysis_fingerprint"] != expected_fingerprint
            ):
                raise ValueError("analysis context expired: source/annotations changed")
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
        matches = (
            graph["_indexes"].symbols.get(symbol, [])
            if "_indexes" in graph
            else [
                n
                for n in nodes
                if symbol in {n["id"], n["identity"], n["qualified_name"]}
            ]
        )
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

    def search(
        self,
        query: str = "",
        limit: int = 50,
        *,
        symbol_type=None,
        path=None,
        decorator=None,
        route=None,
        owner=None,
        entry_kind=None,
        context_entry=None,
        _graph=None,
    ) -> list[dict]:
        graph = _graph if _graph is not None else self._query_graph(context_entry)
        entries = {e["id"]: e for e in graph["entry_points"]["entries"]}
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
                    "symbol_type",
                    "decorators",
                    "class_id",
                )
                if k in n
            }
            | {
                "entry_kinds": entries.get(n["id"], {}).get("entry_kinds", []),
                "entry_reasons": entries.get(n["id"], {}).get("reasons", []),
            }
            for n in graph["nodes"]
            if query.lower() in n["qualified_name"].lower()
            and (symbol_type is None or n["symbol_type"] == symbol_type)
            and (path is None or fnmatch.fnmatch(n["path"], path))
            and (
                decorator is None
                or any(decorator in d for d in n.get("decorators", []))
            )
            and (
                route is None
                or any(
                    route in r["detail"]
                    for r in entries.get(n["id"], {}).get("reasons", [])
                    if r["kind"] == "framework-registration"
                )
            )
            and (
                entry_kind is None
                or entry_kind in entries.get(n["id"], {}).get("entry_kinds", [])
            )
            and (
                owner is None
                or owner in n["qualified_name"].rsplit(".", 1)[0]
                or (n["symbol_type"] == "class" and owner in n["qualified_name"])
            )
        ][:limit]

    def references(
        self, query, *, path=None, kind=None, limit=50, offset=0, context_entry=None
    ):
        if (
            limit < 1
            or limit > 2000
            or offset < 0
            or kind not in {None, "definition", "read", "write"}
        ):
            raise ValueError("invalid reference query range/kind")
        graph = self._query_graph(context_entry)
        matches = search_references(graph.get("references", []), query, path, kind)
        return {
            "result": copy.deepcopy(matches[offset : offset + limit]),
            "total": len(matches),
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "snapshot": graph["snapshot"],
            "analysis_fingerprint": graph.get("analysis_fingerprint")
            or digest(json.dumps([graph["snapshot"], self._read_annotations()[0]])),
            "project_path": str(self.root),
        }

    def entries(self, limit: int = 50, offset: int = 0) -> dict:
        """List detected startup and candidate entry points from the index."""
        if not 1 <= limit <= 2000 or offset < 0:
            raise ValueError("limit 必须为 1..2000；offset 必须 >= 0")
        graph = self._ensure_snapshot()
        discovered = graph["entry_points"]
        total = len(discovered["entries"])
        return copy.deepcopy(
            discovered
            | {
                "entries": discovered["entries"][offset : offset + limit],
                "total": total,
                "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "project_path": str(self.root),
                "snapshot": graph["snapshot"],
                "analysis_fingerprint": graph.get("analysis_fingerprint")
                or digest(json.dumps([graph["snapshot"], self._read_annotations()[0]])),
                "cache_hit": graph["cache_hit"],
                "note": "静态识别的入口，不保证覆盖动态注册或运行时插件；"
                "无内部调用者也可能表示未使用的函数。",
            }
        )

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
        direction: str = "downstream",
    ) -> dict:
        if depth < -1 or max_nodes < 1:
            raise ValueError("depth 必须 >= -1；max_nodes 必须 >= 1")
        if direction not in {"downstream", "upstream"}:
            raise ValueError("direction 必须为 downstream 或 upstream")
        graph = self._query_graph(context_entry)
        root = self.select(graph, symbol)
        indexes = graph["_indexes"]
        by_id = indexes.by_id
        origin, destination = (
            ("target", "source") if direction == "upstream" else ("source", "target")
        )
        adjacency = indexes.incoming if direction == "upstream" else indexes.outgoing

        def adjacent(sid):
            edges = adjacency.get(sid, [])
            if internal_only:
                edges = [
                    e for e in edges if by_id[e[destination]]["kind"] != "external"
                ]
            if prefer_internal:
                edges = sorted(
                    edges, key=lambda e: by_id[e[destination]]["kind"] != "internal"
                )
            return edges

        visited = {root["id"]: 0}
        queue = deque([(root["id"], None, ())])
        processed = set()
        allowed_edges = {}
        callback_candidates = {}
        unknown_callback_scopes = set()
        omitted = set()
        omission_reasons = {}
        deferred = {}
        while queue:
            sid, incoming, environment_key = queue.popleft()
            environment = dict(environment_key)
            state = (sid, incoming, environment_key)
            if state in processed:
                continue
            processed.add(state)
            for edge in adjacent(sid):
                bindings = edge.get("callsite_bindings", [])
                matching = [
                    b
                    for b in bindings
                    if (b["caller_id"], b["line"], b["column"]) == incoming
                ]
                parameters = edge.get("callback_parameters", [])
                contextual_values = {
                    value
                    for parameter in parameters
                    for value in environment.get(parameter, ())
                }
                has_context = bool(parameters) and all(
                    p in environment for p in parameters
                )
                if direction == "downstream" and has_context and not contextual_values:
                    unknown_callback_scopes.add(sid)
                selected = (
                    (
                        bool(
                            contextual_values
                            & set(edge.get("callback_value_ids", [edge["target"]]))
                        )
                        if contextual_values
                        else edge["status"] in {"unresolved", "manual"}
                    )
                    if has_context
                    else any(
                        edge.get("original_target", edge["target"]) in b["target_ids"]
                        for b in matching
                    )
                )
                if (
                    has_context
                    and edge["status"] in {"unresolved", "manual"}
                    and any(
                        b["unknown"]
                        and edge.get("original_target", edge["target"])
                        in b["target_ids"]
                        for b in matching
                    )
                ):
                    selected = True
                    unknown_callback_scopes.add(sid)
                if (
                    direction == "downstream"
                    and incoming is not None
                    and (has_context or matching)
                    and not selected
                ):
                    callback_candidates[edge["id"]] = edge
                    continue
                allowed_edges[edge["id"]] = edge.copy()
                target = edge[destination]
                next_incoming = (edge["source"], edge["line"], edge["column"])
                next_environment = tuple(
                    sorted(
                        (
                            name,
                            tuple(
                                sorted(
                                    {
                                        value
                                        for parameter in binding["source_parameters"]
                                        for value in environment.get(parameter, ())
                                    }
                                )
                            )
                            if binding["source_parameters"]
                            and all(
                                p in environment for p in binding["source_parameters"]
                            )
                            else tuple(binding["value_ids"]),
                        )
                        for name, binding in edge.get("argument_bindings", {}).items()
                    )
                )
                if direction == "upstream":
                    next_environment = ()
                if target in visited:
                    if depth < 0 or visited[sid] < depth:
                        queue.append((target, next_incoming, next_environment))
                    continue
                if prefer_internal and by_id[target]["kind"] == "external":
                    if depth >= 0 and visited[sid] >= depth:
                        omitted.add(target)
                        omission_reasons[edge["id"]] = "depth_limit"
                    else:
                        distance = visited[sid] + 1
                        deferred[target] = min(distance, deferred.get(target, distance))
                    continue
                if (depth >= 0 and visited[sid] >= depth) or len(visited) >= max_nodes:
                    omitted.add(target)
                    omission_reasons[edge["id"]] = (
                        "depth_limit"
                        if depth >= 0 and visited[sid] >= depth
                        else "node_limit"
                    )
                    continue
                visited[target] = visited[sid] + 1
                queue.append((target, next_incoming, next_environment))
        for target, distance in sorted(deferred.items(), key=lambda item: item[1]):
            if len(visited) < max_nodes:
                visited[target] = distance
            else:
                omitted.add(target)
                for edge in allowed_edges.values():
                    if edge[destination] == target:
                        omission_reasons[edge["id"]] = "node_limit"
        selected_edges = list(allowed_edges.values())
        indexed_counts = {
            e["id"]: e["resolution"]["candidate_count"] for e in selected_edges
        }
        describe_edges(selected_edges, by_id)
        for edge in selected_edges:
            resolution = edge["resolution"]
            indexed = indexed_counts[edge["id"]]
            resolution["indexed_candidate_count"] = indexed
            if indexed != resolution["candidate_count"]:
                resolution["summary"] = (
                    f"当前查询路径有 {resolution['candidate_count']} 个静态候选目标；"
                    f"同一调用点在索引中共有 {indexed} 个候选。运行时选择仍需确认。"
                )
        # Union edges selected by every reached call context, including cycles.
        edges = [
            e
            for e in allowed_edges.values()
            if e["source"] in visited
            and e["target"] in visited
            and (not internal_only or by_id[e[destination]]["kind"] != "external")
        ]
        nodes = []
        node_reports, completeness, recovery = report(
            graph,
            visited,
            list(allowed_edges.values()),
            edges,
            direction,
            {
                "project_path": str(self.root),
                "context_entry": context_entry,
                "excludes": self.excludes,
                "scan_mode": self.scan_mode,
                "internal_only": internal_only,
                "prefer_internal": prefer_internal,
            },
            omission_reasons,
            unknown_callback_scopes,
            indexes,
        )
        count_field = "incoming_count" if direction == "upstream" else "outgoing_count"
        relation_counts = Counter(e[origin] for e in allowed_edges.values())
        candidates_by_source = defaultdict_list(
            [
                edge
                for eid, edge in callback_candidates.items()
                if eid not in allowed_edges
            ],
            "source",
        )
        for sid, distance in visited.items():
            node = by_id[sid].copy()
            node["completeness"] = node_reports[sid]
            if sid in unknown_callback_scopes:
                node["callback_context_unknown"] = True
            candidates = candidates_by_source.get(sid, [])
            if candidates:
                node["callback_candidates"] = [
                    {
                        "target_id": e["target"],
                        "target": by_id[e["target"]]["qualified_name"],
                        "line": e["line"],
                        "column": e["column"],
                        "evidence": e["evidence"],
                    }
                    for e in candidates
                ]
                node["callback_candidate_count"] = len(candidates)
                node["callback_candidates_query"] = {
                    "symbol": sid,
                    "depth": 1,
                    "note": "展开包装器自身可查看所有调用点的候选与来源。",
                }
            if not include_code:
                node.pop("code", None)
            nodes.append(
                node
                | {
                    "depth": distance,
                    count_field: relation_counts[sid],
                }
            )
        return copy.deepcopy(
            {
                "entry": root["id"],
                **({"direction": direction} if direction == "upstream" else {}),
                "project_path": str(self.root),
                "nodes": nodes,
                "edges": edges,
                "completeness": completeness,
                "recovery": recovery,
                "snapshot": graph["snapshot"],
                "analysis_fingerprint": graph.get("analysis_fingerprint")
                or digest(json.dumps([graph["snapshot"], self._read_annotations()[0]])),
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
                        e[destination]
                        for e in graph["edges"]
                        if internal_only
                        and e[origin] in visited
                        and by_id[e[destination]]["kind"] == "external"
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
        )

    def callers(
        self,
        symbol: str,
        depth: int = 1,
        max_nodes: int = 500,
        include_code: bool = False,
        *,
        context_entry: str | None = None,
        prefer_internal: bool = False,
        internal_only: bool = False,
    ) -> dict:
        """Find upstream callers, retaining caller-to-callee edge orientation.

        depth=1 returns direct callers; -1 follows all reachable callers within
        max_nodes. Synthetic constructor/callback edges and candidates retain
        their original status, reason and evidence.
        """
        return self.expand(
            symbol,
            depth,
            max_nodes,
            include_code,
            context_entry=context_entry,
            prefer_internal=prefer_internal,
            internal_only=internal_only,
            direction="upstream",
        )

    def context(
        self,
        symbol: str,
        budget: int = 12000,
        depth: int = -1,
        include_code: bool = False,
        *,
        context_entry: str | None = None,
        include_external_docs: bool = False,
        direction: str = "downstream",
        _graph: dict | None = None,
    ) -> str:
        if budget < 256:
            raise ValueError("字符预算至少为 256")
        graph = (
            _graph
            if _graph is not None
            else self.expand(
                symbol,
                depth=depth,
                include_code=include_code,
                context_entry=context_entry,
                prefer_internal=True,
                direction=direction,
            )
        )
        lines = [
            f"# Callmap: {symbol}",
            f"Snapshot: {graph['snapshot']}",
            "静态可能调用关系；candidate 为候选，unresolved 为待确认。",
            *(["查询方向: upstream"] if direction == "upstream" else []),
            f"图裁剪: {graph['truncated']}；解析错误: {len(graph['errors'])}；"
            f"补充复核: {len(graph['annotation_warnings'])}",
            f"推断达到上限: {graph['inference_incomplete']}；"
            "候选边不代表运行时必然调用。",
            "完整度仅统计已索引的静态关系；"
            f"相关未解析调用点: {graph['completeness']['unresolved_callsite_count']}；"
            "索引内未解析调用点: "
            f"{graph['completeness']['unresolved_index_callsite_count']}。",
        ]
        for record in graph["recovery"][:5]:
            followup = record["next_query"]
            target = followup["arguments"].get(
                "symbol", followup["arguments"].get("query", "index")
            )
            lines.append(
                f"待补齐: {record['symbol']} [{record['kind']}]；下一步 "
                f"{followup['tool']} "
                f"{target}"
            )
        upstream = direction == "upstream"
        outgoing = defaultdict_list(graph["edges"], "target" if upstream else "source")
        used = len("\n".join(lines))
        for node in graph["nodes"]:
            block = [
                f"\n## {node['qualified_name']} [{node['id']}] ({node['kind']})",
                f"{node['path']}:{node['line']} | {node['signature']}",
            ]
            for name, label in (("callers", "直接调用者"), ("calls", "直接下游")):
                info = node["completeness"][name]
                block.append(
                    f"{label}: 没有识别到关系（当前索引范围）。"
                    if info["state"] == "none_identified"
                    else f"{label}: {info['state']}；识别 {info['identified']} 条，"
                    f"当前图列出 {info['listed']} 条。"
                )
            block += [
                f"- {e['expression']} @L{e['line']} "
                + (f"<- {e['source']} " if upstream else f"-> {e['target']} ")
                + f"[{e['status']}; {e['reason']}]"
                + f" {e['resolution']['summary']}"
                + f" 解析依据: {', '.join(e['resolution']['basis'])}"
                + f" 成立条件: {'；'.join(e['resolution']['conditions'])}"
                + f" 未知部分: {'；'.join(e['resolution']['unknowns'])}"
                + f" 执行方式: {e['callsite']['execution']['mode']}"
                + ("（等待结果）" if e["callsite"]["execution"]["awaited"] else "")
                + " 实参: "
                + ", ".join(
                    f"{a.get('parameter') or a.get('position', '?')}={a['expression']}"
                    + ("（默认）" if a["kind"] == "default" else "")
                    for a in e["callsite"]["arguments"]
                )
                + " 路径: "
                + "; ".join(
                    f"{c['kind']}:{c.get('expression', c.get('requirement', ''))}"
                    + (f" truthy={c['truthy']}" if "truthy" in c else "")
                    for c in e["callsite"]["conditions"]
                )
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
        graph = self._ensure_snapshot(force)
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
        return copy.deepcopy(
            {
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
                "analysis_fingerprint": graph.get("analysis_fingerprint")
                or digest(json.dumps([graph["snapshot"], self._read_annotations()[0]])),
                "cache_hit": graph["cache_hit"],
                "build_seconds": graph["build_seconds"],
                "analysis": graph.get("analysis", {}),
                "cache_dir": str(self.cache),
                "coverage": coverage,
                "entry_points": {
                    key: value
                    for key, value in graph["entry_points"].items()
                    if key != "entries"
                },
            }
        )


def defaultdict_list(items: list[dict], key: str) -> dict[str, list]:
    result: dict[str, list] = {}
    for item in items:
        result.setdefault(item[key], []).append(item)
    return result
