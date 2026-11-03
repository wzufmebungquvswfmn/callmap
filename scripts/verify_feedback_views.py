"""Measure bounded responses and verify complete pagination on real call graphs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def verify(project: Path, cache: Path, output: Path) -> dict:
    store = GraphStore(project, cache)
    results = []
    for symbol in (
        "RunnerFactory.get_runner",
        "EvaluatorFactory.get_evaluator",
        "app.utils.logging._ctx_prefix",
    ):
        graph = store.expand(symbol, depth=2, max_nodes=200, prefer_internal=True)
        query = {
            "symbol": symbol,
            "project_path": str(project),
            "depth": 2,
            "max_nodes": 200,
            "budget": 20000,
            "prefer_internal": True,
        }
        page = bounded_graph(graph, query)
        first_page_characters = len(serialize(page))
        pages = 0
        seen_edges = set()
        seen_nodes = set()
        while True:
            text = serialize(page)
            assert len(text) <= 20000 and page["response_characters"] == len(text)
            assert all("doc" not in n and "code" not in n for n in page["nodes"])
            ids = {n["id"] for n in page["nodes"]}
            assert all(e["source"] in ids and e["target"] in ids for e in page["edges"])
            seen_nodes.update(ids)
            seen_edges.update(e["id"] for e in page["edges"])
            pages += 1
            if page["next_cursor"] is None:
                break
            page = bounded_graph(
                graph,
                query,
                cursor=page["next_cursor"],
                snapshot=page["response_snapshot"],
            )
        assert seen_edges == {e["id"] for e in graph["edges"]}
        assert seen_nodes == {n["id"] for n in graph["nodes"]}
        results.append(
            {
                "symbol": symbol,
                "full_json_characters": len(serialize(graph)),
                "compact_first_page_characters": first_page_characters,
                "pages_to_fetch_entire_selected_graph": pages,
                "nodes": len(seen_nodes),
                "edges": len(seen_edges),
                "graph_truncated": graph["graph_truncated"],
                "pagination_complete": True,
            }
        )
    report = {"project": str(project), "budget_per_response": 20000, "results": results}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--cache", type=Path, default=Path("artifacts/feedback-flux-verification/index")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/feedback-view-report.json")
    )
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.project.resolve(), args.cache, args.output),
            ensure_ascii=False,
            indent=2,
        )
    )
