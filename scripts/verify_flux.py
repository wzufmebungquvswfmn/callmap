"""Read-only integration checks against flux_test_be; never executes its code."""

from __future__ import annotations

import argparse
import ast
import json
import time
from pathlib import Path

from callmap.store import GraphStore


def verify(project: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    store = GraphStore(project, output / "index")
    started = time.perf_counter()
    cold = store.stats(force=True)
    cold_seconds = time.perf_counter() - started
    started = time.perf_counter()
    warm = store.stats()
    warm_seconds = time.perf_counter() - started
    assert warm["cache_hit"], "Unchanged project should reuse the snapshot"
    assert not cold["errors"], cold["errors"]
    full = store.graph()
    assert len(full["nodes"]) == len({n["id"] for n in full["nodes"]})
    assert len(full["edges"]) == len({e["id"] for e in full["edges"]})
    ids = {n["id"] for n in full["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in full["edges"])

    factory_name = "app.services.engine.runners.factory.RunnerFactory.get_runner"
    factory = store.expand(factory_name, depth=2)
    by_id = {n["id"]: n for n in factory["nodes"]}
    direct = [
        by_id[e["target"]] for e in factory["edges"] if e["source"] == factory["entry"]
    ]
    expected = {
        "app.utils.logging._ctx_prefix",
        "app.services.engine.runners.factory._debug_runner_factory_runtime",
        "app.services.engine.runners.factory._load_builtin_runner_module",
        "app.services.engine.runners.factory._ensure_builtin_runners_registered",
    }
    assert expected <= {n["qualified_name"] for n in direct}
    cls_edges = [
        e
        for e in factory["edges"]
        if e["source"] == factory["entry"] and e["expression"] == "cls"
    ]
    runner_names = {by_id[e["target"]]["qualified_name"] for e in cls_edges}
    expected_runners = decorated_classes(
        project, "app/services/engine/runners", "register_runner"
    )
    assert runner_names == expected_runners, (runner_names, expected_runners)
    assert all(e["status"] == "candidate" and e["evidence"] for e in cls_edges)
    evaluators = store.expand(
        "app.services.engine.evaluators.factory.EvaluatorFactory.get_evaluator", depth=1
    )
    evaluator_nodes = {n["id"]: n for n in evaluators["nodes"]}
    evaluator_edges = [
        e
        for e in evaluators["edges"]
        if e["source"] == evaluators["entry"] and e["expression"] == "cls"
    ]
    evaluator_names = {
        evaluator_nodes[e["target"]]["qualified_name"] for e in evaluator_edges
    }
    assert evaluator_names == decorated_classes(
        project, "app/services/engine/evaluators", "register_evaluator"
    )

    recursion = store.expand("app.services.task.service._remove_internal_error_fields")
    recursive_edges = [
        e
        for e in recursion["edges"]
        if e["source"] == e["target"] == recursion["entry"]
    ]
    assert len(recursive_edges) == 2, "Two recursive call sites share one function node"
    assert sum(n["id"] == recursion["entry"] for n in recursion["nodes"]) == 1

    context = store.expand("app.utils.logging._ctx_prefix")
    assert {
        "app.utils.context.get_current_context",
        "app.utils.context.RequestContext.to_log_prefix",
        "app.utils.context.RequestContext.to_log_parts",
    } <= {n["qualified_name"] for n in context["nodes"]}
    assert "contextvars.ContextVar.get" in {
        n["qualified_name"] for n in context["nodes"]
    }
    assert "logging.Logger.info" in {n["qualified_name"] for n in direct}
    assert full["analysis"]["argument_converged"], full["analysis"]

    for name, graph in (
        ("runner-factory", factory),
        ("recursion", recursion),
        ("logging-context", context),
        ("evaluator-factory", evaluators),
    ):
        (output / f"{name}.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2)
        )
    (output / "runner-context.md").write_text(store.context(factory_name))
    report = cold | {
        "cold_wall_seconds": round(cold_seconds, 3),
        "warm_wall_seconds": round(warm_seconds, 3),
        "warm_cache_hit": warm["cache_hit"],
        "runner_candidates": sorted(runner_names),
        "evaluator_candidates": sorted(evaluator_names),
        "checks": [
            "No syntax errors in the scanned files",
            "Unique node IDs and unique edge IDs, no dangling edges",
            "RunnerFactory cross-module direct calls match source",
            "Factory candidates match indexed registration decorators",
            "Registry edges are candidates with registration evidence",
            "Logger and ContextVar methods resolve without executing target code",
            "Argument/return/registry propagation converged",
            "Two recursive call sites point back to the same function node",
            "Return annotation links logging context across modules",
            "Unchanged project reuses the SQLite snapshot",
        ],
        "scope": "Static analysis only; target application and its tests were not run",
    }
    baseline_path = Path(__file__).resolve().parents[1] / "artifacts/baseline-v1.json"
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())

        def key(edge):
            return edge["source"], edge["line"], edge["column"], edge["expression"]

        before = {key(e) for e in baseline["calls"] if e["status"] == "unresolved"}
        after = {key(e) for e in full["edges"] if e["status"] == "unresolved"}
        report["comparison"] = {
            "baseline_unresolved_callsites": len(before),
            "current_unresolved_callsites": len(after),
            "recovered_callsites": len(before - after),
            "newly_unresolved_callsites": len(after - before),
            "net_reduction": len(before) - len(after),
            "note": "Candidate inference is not measured runtime accuracy",
        }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    return report


def decorated_classes(project: Path, directory: str, decorator: str) -> set[str]:
    result = set()
    for path in (project / directory).rglob("*.py"):
        module = ".".join(path.relative_to(project).with_suffix("").parts)
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(d, ast.Name) and d.id == decorator
                for d in node.decorator_list
            ):
                result.add(module + "." + node.name)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/flux-verification")
    )
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.project.resolve(), args.output.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )
