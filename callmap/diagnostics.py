"""Specific evidence gaps and source-oriented follow-up actions."""


def edge_gaps(edge, target):
    resolution = edge["resolution"]
    result = []
    source = edge["source"]
    if edge["status"] == "unresolved":
        kind = (
            "receiver_type_unknown"
            if "." in edge["expression"]
            else "callback_target_unknown"
        )
        result.append(
            {
                "kind": kind,
                "detail": "查构造、赋值或参数传入来源",
                "next_query": {"tool": "read_symbol", "arguments": {"symbol": source}},
            }
        )
    if "registry" in resolution["basis"]:
        result.append(
            {
                "kind": "registry_selection",
                "detail": "核对注册项、选择键及加载路径",
                "next_query": {"tool": "read_symbol", "arguments": {"symbol": source}},
            }
        )
    for dependency in resolution.get("configuration_sources", []):
        result.append(
            {
                "kind": "configuration_value_unknown",
                "detail": dependency["expression"] + " 的实际配置值未知",
                "sources": dependency["sources"],
                "next_query": {
                    "tool": "find_references",
                    "arguments": {
                        "query": dependency.get("query", dependency["expression"])
                    },
                },
            }
        )
    if target["kind"] == "external" and (
        target.get("declaration_available") is False
        or target.get("doc_source") == "missing"
    ):
        result.append(
            {
                "kind": "external_library_source",
                "detail": "外部声明/实现未索引；使用 --library-root 补充后建立新上下文",
                "next_query": {"tool": "index_status", "arguments": {}},
            }
        )
    if resolution.get("provenance_truncated"):
        result.append(
            {
                "kind": "provenance_limit",
                "detail": "来源传播达到边界；继续核对源函数",
                "next_query": {"tool": "read_symbol", "arguments": {"symbol": source}},
            }
        )
    return result
