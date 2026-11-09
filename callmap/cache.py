"""Caches validated by filesystem state, with no time-based stale window."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path


def file_stamp(path: Path) -> tuple:
    stat = path.stat()
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


class FileHashes:
    def __init__(self):
        self.values: dict[Path, tuple[tuple, str]] = {}

    def digest(self, path: Path, force: bool = False) -> str:
        try:
            before = file_stamp(path)
            cached = self.values.get(path)
            if not force and cached is not None and cached[0] == before:
                return cached[1]
            result = hashlib.sha256(path.read_bytes()).hexdigest()
            if file_stamp(path) == before:
                self.values[path] = (before, result)
            else:
                self.values.pop(path, None)
            return result
        except OSError:
            self.values.pop(path, None)
            raise


class GraphIndexes:
    """Internal indexes hold references to an immutable graph view."""

    def __init__(self, graph: dict):
        self.by_id = {n["id"]: n for n in graph["nodes"]}
        self.symbols = defaultdict(list)
        for node in graph["nodes"]:
            for value in {node["id"], node["identity"], node["qualified_name"]}:
                self.symbols[value].append(node)
        self.incoming = defaultdict(list)
        self.outgoing = defaultdict(list)
        self.scope = set(graph.get("context_scope", []))
        unresolved = set()
        for edge in graph["edges"]:
            self.incoming[edge["target"]].append(edge)
            self.outgoing[edge["source"]].append(edge)
            self.scope.update((edge["source"], edge["target"]))
            if edge["status"] == "unresolved":
                unresolved.add(
                    (edge["source"], edge["line"], edge["column"], edge["expression"])
                )
        self.unresolved_count = len(unresolved)
