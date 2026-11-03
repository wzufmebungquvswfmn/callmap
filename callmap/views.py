"""Bounded, paginated call graph responses with complete edge endpoints."""

from __future__ import annotations

import json

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
    "is_test",
)


def serialize(payload: dict) -> str:
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
            for key in ("doc", "automatic_doc", "code", "annotation"):
                if node.pop(key, None) is not None:
                    omitted.append(key)
            node["content_omitted"] = omitted
        nodes[node["id"]] = node
    edges = []
    for original in graph["edges"]:
        edge = original.copy()
        if compact:
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
            nodes[e["source"]]["depth"],
            nodes[e["target"]]["kind"] == "external",
            e["line"],
            e["column"],
            e["id"],
        )
    )
    incident = {e[key] for e in edges for key in ("source", "target")}
    isolated = [sid for sid in nodes if sid not in incident and sid != graph["entry"]]
    records = [("edge", e) for e in edges] + [("node", sid) for sid in isolated]
    request_snapshot = digest(
        serialize(
            {
                "snapshot": graph["snapshot"],
                "entry": graph["entry"],
                "context": graph.get("context"),
                "edges": [e["id"] for e in edges],
                "nodes": list(nodes),
                "compact": compact,
                "include_docs": include_docs,
            }
        )
    )
    if snapshot is not None and snapshot != request_snapshot:
        raise ValueError("分页快照或查询范围已变化；请从 cursor=0 重新展开")
    if not 0 <= cursor <= len(records):
        raise ValueError("cursor 超出本次展开的分页范围")
    payload = {
        "entry": graph["entry"],
        "project_path": graph.get("project_path"),
        "nodes": [nodes[graph["entry"]]],
        "edges": [],
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
        "图达到上限时从 graph_omitted_frontier 的目标 ID 继续展开。",
    }
    visible = {graph["entry"]}

    def finish(end):
        more = end < len(records)
        payload["response_truncated"] = more
        payload["next_cursor"] = end if more else None
        payload["continuation"] = (
            {
                "tool": "expand_calls",
                "arguments": query | {"cursor": end, "snapshot": request_snapshot},
            }
            if more
            else None
        )
        payload["response_omitted_nodes"] = len(nodes) - len(visible)
        payload["response_omitted_edges"] = len(edges) - len(payload["edges"])
        # Stabilize the digit count of the self-reported text size.
        for _ in range(3):
            payload["response_characters"] = len(serialize(payload))

    finish(cursor)
    if len(serialize(payload)) > budget:
        raise ValueError(
            "预算不足以容纳入口和分页信息；请增加 budget 或使用 read_symbol"
        )
    end = cursor
    while end < len(records):
        kind, record = records[end]
        required = {record["source"], record["target"]} if kind == "edge" else {record}
        new = [sid for sid in nodes if sid in required - visible]
        for sid in new:
            payload["nodes"].append(nodes[sid])
        visible.update(new)
        if kind == "edge":
            payload["edges"].append(record)
        finish(end + 1)
        if len(serialize(payload)) > budget and end == cursor:
            # Optional content must not prevent the first complete graph record
            # from fitting. Preserve it when possible, otherwise defer whole
            # blocks to read_symbol and report exactly which fields were omitted.
            for node in payload["nodes"]:
                omitted = list(node.get("content_omitted", []))
                for key in ("doc", "automatic_doc", "code", "annotation"):
                    if node.pop(key, None) is not None and key not in omitted:
                        omitted.append(key)
                if omitted:
                    node["content_omitted"] = omitted
            finish(end + 1)
        if len(serialize(payload)) > budget:
            if kind == "edge":
                payload["edges"].pop()
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
