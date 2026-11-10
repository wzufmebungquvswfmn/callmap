"""Bounded, paginated call graph responses with complete edge endpoints."""

from __future__ import annotations

import json
from copy import deepcopy

from callmap.analyzer import digest

NODE_FIELDS = (
    "id",
    "qualified_name",
    "kind",
    "symbol_type",
    "path",
    "line",
    "end_line",
    "signature",
    "depth",
    "outgoing_count",
    "incoming_count",
    "is_test",
    "callback_candidate_count",
    "callback_candidates_query",
    "callback_context_unknown",
    "completeness",
)


def serialize(payload: dict) -> str:
    if payload.get("view") == "compact" or payload.get("serialization") == "compact":
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(payload, ensure_ascii=False, indent=2)


def bounded_graph(
    graph: dict,
    query: dict,
    *,
    budget: int = 20000,
    compact: bool = True,
    include_docs: bool = False,
    cursor: int = 0,
    snapshot: str | None = None,
) -> dict:
    """Budget the exact JSON text, not token estimates or an SDK's duplicate output."""
    if budget < 2000:
        raise ValueError("响应字符预算至少为 2000；更小的读取请使用 read_symbol")
    nodes = {}
    for original in graph["nodes"]:
        node = (
            {k: original[k] for k in NODE_FIELDS if k in original}
            if compact
            else original.copy()
        )
        if "completeness" in original:
            node["completeness"] = deepcopy(original["completeness"])
            if compact:
                for info in node["completeness"].values():
                    info.pop("next_query", None)
                    if not info.get("hidden_external"):
                        info.pop("hidden_external", None)
        if compact and len(node.get("signature", "")) > 300:
            node["signature"] = node["signature"][:300] + "…"
            node["signature_truncated"] = True
        node["doc_available"] = bool(original.get("doc"))
        if "code" in original:
            node["code"] = original["code"]
        if include_docs:
            node["doc"] = original.get("doc", "")
        else:
            node.pop("doc", None)
            node.pop("automatic_doc", None)
            if "annotation" in node:
                node["annotation"] = {
                    k: v for k, v in node["annotation"].items() if k != "doc"
                }
        if len(serialize(node)) > budget // 2:
            omitted = []
            for key in (
                "doc",
                "automatic_doc",
                "code",
                "annotation",
                "callback_candidates",
            ):
                if node.pop(key, None) is not None:
                    omitted.append(key)
            node["content_omitted"] = omitted
        nodes[node["id"]] = node
    edges = []
    for original in graph["edges"]:
        edge = original.copy()
        if compact:
            edge = deepcopy(edge)
            for key in ("provenance", "configuration_sources", "gaps"):
                values = edge.get("resolution", {}).get(key, [])
                if len(values) > 3:
                    edge["resolution"][key] = values[:3]
                    edge["resolution"][key + "_omitted_count"] = len(values) - 3
            edge.pop("argument_bindings", None)
            edge.pop("callback_value_ids", None)
            bindings = edge.get("callsite_bindings", [])
            if len(bindings) > 3:
                edge["callsite_bindings"] = bindings[:3]
                edge["callsite_bindings_omitted_count"] = len(bindings) - 3
            evidence = edge.get("evidence", [])
            edge["evidence"] = [e.copy() for e in evidence[:3]]
            if len(evidence) > 3:
                edge["evidence_omitted_count"] = len(evidence) - 3
            for item in edge["evidence"]:
                for key in ("argument", "value"):
                    if len(str(item.get(key, ""))) > 256:
                        item[key] = str(item[key])[:256] + "…"
                        item[key + "_truncated"] = True
            if len(edge["expression"]) > 512:
                edge["expression"] = edge["expression"][:512] + "…"
                edge["expression_truncated"] = True
        edges.append(edge)
    edges.sort(
        key=lambda e: (
            nodes[e["target" if graph.get("direction") == "upstream" else "source"]][
                "depth"
            ],
            nodes[e["target"]]["kind"] == "external",
            e["line"],
            e["column"],
            e["id"],
        )
    )
    incident = {e[key] for e in edges for key in ("source", "target")}
    isolated = [sid for sid in nodes if sid not in incident and sid != graph["entry"]]
    recovery = deepcopy(graph.get("recovery", []))
    records = (
        [("edge", e) for e in edges]
        + [("node", sid) for sid in isolated]
        + [("recovery", r) for r in recovery]
    )
    request_snapshot = digest(
        serialize(
            {
                "snapshot": graph["snapshot"],
                "entry": graph["entry"],
                "context": graph.get("context"),
                # Hash source records before budget-driven omission so content
                # changes invalidate continuations even when topology is stable.
                "edges": graph["edges"],
                "nodes": graph["nodes"],
                "metadata": {
                    key: graph.get(key)
                    for key in (
                        "truncated",
                        "omitted_frontier",
                        "hidden_external_node_count",
                        "inference_incomplete",
                        "errors",
                        "annotation_warnings",
                        "completeness",
                        "recovery",
                    )
                },
                "compact": compact,
                "include_docs": include_docs,
                "direction": graph.get("direction", "downstream"),
            }
        )
    )
    if snapshot is not None and snapshot != request_snapshot:
        raise ValueError("分页快照或查询范围已变化；请从 cursor=0 重新展开")
    if not 0 <= cursor <= len(records):
        raise ValueError("cursor 超出本次展开的分页范围")
    payload = {
        "entry": graph["entry"],
        **(
            {"analysis_context": graph["analysis_context"]}
            if graph.get("analysis_context")
            else {}
        ),
        **({"direction": graph["direction"]} if "direction" in graph else {}),
        "project_path": graph.get("project_path"),
        "nodes": [nodes[graph["entry"]]],
        "edges": [],
        "completeness": graph.get("completeness", {}),
        "recovery": [],
        "recovery_total": len(recovery),
        "snapshot": graph["snapshot"],
        "response_snapshot": request_snapshot,
        "cache_hit": graph["cache_hit"],
        "truncated": graph["truncated"],
        "graph_truncated": graph["truncated"],
        "omitted_frontier_count": graph["omitted_frontier_count"],
        "graph_omitted_frontier": graph.get("omitted_frontier", []),
        "hidden_external_node_count": graph.get("hidden_external_node_count", 0),
        "view": "compact" if compact else "full",
        "include_docs": include_docs,
        "response_truncated": False,
        "response_characters": 0,
        "response_budget": budget,
        "response_total_nodes": len(nodes),
        "response_total_edges": len(edges),
        "response_omitted_nodes": 0,
        "response_omitted_edges": 0,
        "cursor": cursor,
        "next_cursor": None,
        "continuation": None,
        "context": graph.get("context"),
        "context_entry_reaches_symbol": graph["context_entry_reaches_symbol"],
        "external_nodes_hidden": graph["external_nodes_hidden"],
        "inference_incomplete": graph["inference_incomplete"],
        "errors": graph["errors"][:3],
        "error_count": len(graph["errors"]),
        "annotation_warnings": graph["annotation_warnings"][:3],
        "annotation_warning_count": len(graph["annotation_warnings"]),
        "read_more": "read_symbol 获取完整源码/文档；compact=false 查看完整证据；"
        "图达到上限时从 graph_omitted_frontier 的节点 ID 继续按当前方向查询。",
    }
    visible = {graph["entry"]}

    def finish(end):
        more = end < len(records)
        payload["response_truncated"] = more
        payload["next_cursor"] = end if more else None
        payload["continuation"] = (
            {
                "tool": (
                    "find_callers"
                    if graph.get("direction") == "upstream"
                    else "expand_calls"
                ),
                "arguments": query | {"cursor": end, "snapshot": request_snapshot},
            }
            if more
            else None
        )
        payload["response_omitted_nodes"] = len(nodes) - len(visible)
        payload["response_omitted_edges"] = len(edges) - len(payload["edges"])
        for node in payload["nodes"]:
            for name, endpoint in (("calls", "source"), ("callers", "target")):
                info = node.get("completeness", {}).get(name)
                if info is not None:
                    listed = sum(e[endpoint] == node["id"] for e in payload["edges"])
                    info["response_listed"] = listed
                    info["response_complete"] = (
                        info["complete"] and listed == info["identified"]
                    )
        # Stabilize the digit count of the self-reported text size.
        for _ in range(3):
            payload["response_characters"] = len(serialize(payload))

    finish(cursor)
    if len(serialize(payload)) > budget:
        payload["serialization"] = "compact"
        finish(cursor)
    if len(serialize(payload)) > budget:
        raise ValueError(
            "预算不足以容纳入口和分页信息；请增加 budget 或使用 read_symbol"
        )
    end = cursor
    while end < len(records):
        kind, record = records[end]
        required = (
            {record["source"], record["target"]}
            if kind == "edge"
            else {record["source_id"]}
            if kind == "recovery"
            else {record}
        )
        new = [sid for sid in nodes if sid in required - visible]
        for sid in new:
            payload["nodes"].append(nodes[sid])
        visible.update(new)
        if kind == "edge":
            payload["edges"].append(record)
        elif kind == "recovery":
            payload["recovery"].append(record)
        finish(end + 1)
        if len(serialize(payload)) > budget and end == cursor:
            payload["serialization"] = "compact"
            finish(end + 1)
        if len(serialize(payload)) > budget and end == cursor:
            # Optional content must not prevent the first complete graph record
            # from fitting. Preserve it when possible, otherwise defer whole
            # blocks to read_symbol and report exactly which fields were omitted.
            for node in payload["nodes"]:
                omitted = list(node.get("content_omitted", []))
                for key in (
                    "doc",
                    "automatic_doc",
                    "code",
                    "annotation",
                    "callback_candidates",
                ):
                    if node.pop(key, None) is not None and key not in omitted:
                        omitted.append(key)
                if omitted:
                    node["content_omitted"] = omitted
                for info in node.get("completeness", {}).values():
                    if info.pop("next_query", None) is not None:
                        omitted.append("completeness.next_query")
                        node["content_omitted"] = omitted
            finish(end + 1)
            if kind == "edge" and len(serialize(payload)) > budget:
                # Retain the basis and execution role even in very small pages.
                omitted = []
                record["resolution"] = record.get("resolution", {}).copy()
                for key in (
                    "conditions",
                    "unknowns",
                    "summary",
                    "provenance",
                    "configuration_sources",
                    "gaps",
                ):
                    if record["resolution"].pop(key, None) is not None:
                        omitted.append("resolution." + key)
                record["callsite"] = record.get("callsite", {}).copy()
                for key in ("arguments", "conditions", "guard_paths"):
                    if record["callsite"].pop(key, None) is not None:
                        omitted.append("callsite." + key)
                record["details_omitted"] = omitted
                if "explanation" in record:
                    record["explanation"] = deepcopy(record["explanation"])
                    for key in ("conditions", "missing", "guard_paths"):
                        values = record["explanation"].get(key, [])
                        if len(values) > 1:
                            record["explanation"][key] = values[:1]
                            record["explanation"][key + "_omitted_count"] = (
                                len(values) - 1
                            )
                finish(end + 1)
            if kind == "recovery" and len(serialize(payload)) > budget:
                sources = record.pop("sources", [])
                record["sources_omitted_count"] = len(sources)
                finish(end + 1)
            if len(serialize(payload)) > budget:
                payload["serialization"] = "compact"
                finish(end + 1)
            if len(serialize(payload)) > budget:
                secondary = (
                    "calls" if graph.get("direction") == "upstream" else "callers"
                )
                for node in payload["nodes"]:
                    if node.get("completeness", {}).pop(secondary, None) is not None:
                        node["content_omitted"] = node.get("content_omitted", []) + [
                            "completeness." + secondary
                        ]
                finish(end + 1)
        if len(serialize(payload)) > budget:
            if kind == "edge":
                payload["edges"].pop()
            elif kind == "recovery":
                payload["recovery"].pop()
            if new:
                del payload["nodes"][-len(new) :]
                visible.difference_update(new)
            finish(end)
            break
        end += 1
    if end == cursor and end < len(records):
        raise ValueError(
            "预算不足以容纳下一条完整调用边；请增加 budget 或 compact=true"
        )
    return payload
