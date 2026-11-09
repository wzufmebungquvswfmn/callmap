"""Completeness within the indexed scope, with actionable recovery queries."""

from __future__ import annotations

from collections import defaultdict

from callmap.cache import GraphIndexes


def group(edges, endpoint):
    result = defaultdict(list)
    for edge in edges:
        result[edge[endpoint]].append(edge)
    return result


def report(
    graph,
    visited,
    allowed,
    shown,
    direction,
    query,
    omissions,
    unknown_callbacks=(),
    indexes=None,
):
    indexes = indexes or GraphIndexes(graph)
    by_id = indexes.by_id
    outgoing = (
        group(allowed, "source") if direction == "downstream" else indexes.outgoing
    )
    incoming = indexes.incoming
    shown_outgoing = group(shown, "source")
    shown_incoming = group(shown, "target")
    scope = indexes.scope
    node_reports = {}
    recovery = []

    def next_query(sid, orientation, degree):
        return {
            "tool": "find_callers" if orientation == "callers" else "expand_calls",
            "arguments": query
            | {
                "symbol": sid,
                "depth": 1,
                "max_nodes": max(200, degree + 1),
            },
        }

    for sid in visited:
        outside = graph.get("context") is not None and sid not in scope
        info = {}
        for name, known, listed in (
            ("calls", outgoing.get(sid, []), shown_outgoing[sid]),
            ("callers", incoming.get(sid, []), shown_incoming[sid]),
        ):
            eligible = [
                e
                for e in known
                if not query["internal_only"]
                or by_id[e["target" if name == "calls" else "source"]]["kind"]
                != "external"
            ]
            listed_ids = {e["id"] for e in listed}
            missing = [e for e in eligible if e["id"] not in listed_ids]
            count = len(eligible)
            info[name] = {
                "identified": count,
                "listed": count - len(missing),
                "complete": not missing and not outside,
                "state": "outside_context"
                if outside
                else "none_identified"
                if count == 0
                else "partially_listed"
                if missing
                else "all_identified_listed",
                "hidden_external": len(known) - len(eligible),
            }
            if missing:
                followup = next_query(sid, name, count)
                info[name]["next_query"] = followup
                if name == ("callers" if direction == "upstream" else "calls"):
                    recovery.append(
                        {
                            "kind": "unexpanded",
                            "source_id": sid,
                            "symbol": by_id[sid]["qualified_name"],
                            "omitted_relation_count": len(missing),
                            "causes": sorted(
                                {
                                    omissions.get(e["id"], "not_expanded")
                                    for e in missing
                                }
                            ),
                            "next_query": followup,
                        }
                    )
        node_reports[sid] = info
        unresolved = [e for e in outgoing.get(sid, []) if e["status"] == "unresolved"]
        contextual_unknown = sid in unknown_callbacks and not unresolved
        if contextual_unknown:
            unresolved = [
                e for e in indexes.outgoing.get(sid, []) if e.get("callback_parameters")
            ]
        if unresolved:
            recovery.append(
                {
                    "kind": "unresolved_input" if contextual_unknown else "unresolved",
                    "source_id": sid,
                    "symbol": by_id[sid]["qualified_name"],
                    "callsite_count": len(
                        {(e["line"], e["column"], e["expression"]) for e in unresolved}
                    ),
                    "callsites": [
                        {
                            "line": e["line"],
                            "column": e["column"],
                            "expression": e["expression"],
                        }
                        for e in unresolved[:5]
                    ],
                    "next_query": {
                        "tool": "read_symbol",
                        "arguments": {
                            key: value
                            for key, value in (query | {"symbol": sid}).items()
                            if key
                            in {
                                "project_path",
                                "context_entry",
                                "excludes",
                                "scan_mode",
                                "symbol",
                            }
                        },
                    },
                }
            )
    coverage = graph.get("coverage", {})
    summary = {
        "scope": "indexed_static_graph",
        "runtime_complete": False,
        "unexpanded_node_count": sum(r["kind"] == "unexpanded" for r in recovery),
        "unresolved_callsite_count": sum(r.get("callsite_count", 0) for r in recovery),
        "unresolved_index_callsite_count": indexes.unresolved_count,
        "parse_error_count": len(graph["errors"]),
        "unknown_callback_scope_count": len(unknown_callbacks),
        "excluded_file_count": len(coverage.get("skipped_files", [])),
        "excluded_directory_count": len(coverage.get("skipped_directories", [])),
        "scan_audit_complete": coverage.get("audit_complete", True),
    }
    return node_reports, summary, recovery
