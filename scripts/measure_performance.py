"""Repeatable same-process query benchmark without executing project code."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def semantic_digest(graph: dict) -> str:
    data = {
        "nodes": sorted(
            (n["id"], n["qualified_name"], n["hash"], n["signature"], n["doc"])
            for n in graph["nodes"]
        ),
        "edges": sorted(graph["edges"], key=lambda e: e["id"]),
        "truncated": graph["truncated"],
        "completeness": graph["completeness"],
        "recovery": graph["recovery"],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def measure(project: Path, cache: Path, symbol: str, repeats: int) -> dict:
    store = GraphStore(project, cache)
    start = time.perf_counter()
    cold = store.stats(force=True)
    result = {
        "project": str(project),
        "backend": "AST, jedi=False",
        "repeats": repeats,
        "files": cold["files"],
        "nodes": cold["nodes"],
        "edges": cold["edges"],
        "cold_seconds": time.perf_counter() - start,
    }
    print(json.dumps({"cold": result}), flush=True)
    operations = {
        "index_status": store.stats,
        "expand_depth_2": lambda: store.expand(symbol, depth=2, max_nodes=200),
        "callers_depth_1": lambda: store.callers(symbol),
        "read_symbol": lambda: (
            store.read_node(symbol)
            if hasattr(store, "read_node")
            else store.select(store.graph(), symbol)
        ),
    }
    for name, operation in operations.items():
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            output = operation()
            samples.append(time.perf_counter() - start)
        result[name] = {
            "seconds": samples,
            "median_seconds": statistics.median(samples),
        }
        if name in {"expand_depth_2", "callers_depth_1"}:
            result[name]["semantic_digest"] = semantic_digest(output)
        print(name, result[name], flush=True)
    start = time.perf_counter()
    store.expand(symbol, depth=2, max_nodes=200, context_entry=symbol)
    result["context_first_seconds"] = time.perf_counter() - start
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        output = store.expand(symbol, depth=2, max_nodes=200, context_entry=symbol)
        samples.append(time.perf_counter() - start)
    result["context_warm"] = {
        "seconds": samples,
        "median_seconds": statistics.median(samples),
        "semantic_digest": semantic_digest(output),
    }
    graph = store.expand(symbol, depth=2, max_nodes=200)
    start = time.perf_counter()
    page = bounded_graph(
        graph, {"symbol": symbol, "project_path": str(project)}, budget=20000
    )
    result["bounded_graph_seconds"] = time.perf_counter() - start
    result["response_chars"] = len(serialize(page))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    data = measure(
        args.project.resolve(), args.cache.resolve(), args.symbol, args.repeats
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(json.dumps(data, ensure_ascii=False, indent=2))
