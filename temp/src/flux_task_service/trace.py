"""Fornax span conversion and call trees migrated from the sibling script."""

import contextlib
from collections import defaultdict
from datetime import UTC, datetime

from flux_task_service import metrics as F


def _span_key(span: dict) -> tuple[str, str]:
    return str(span.get("trace_id") or ""), str(span.get("span_id") or "")


def _span_list(value) -> tuple[list[dict], str]:
    if not isinstance(value, list):
        return [], "span 数据不是列表"
    if any(
        not isinstance(span, dict)
        or not span.get("span_id")
        or not isinstance(span.get("name", span.get("span_name")), str)
        or not span.get("name", span.get("span_name"))
        for span in value
    ):
        return [], "span 数据缺少有效 span_id / name"
    if any(
        span.get(key) is not None and not isinstance(span[key], dict)
        for span in value
        for key in ("attributes", "custom_tags", "usage")
    ):
        return [], "span attributes / custom_tags / usage 不是对象"
    seen = {_span_key(span): span for span in value}
    return list(seen.values()), ""


def _time_ms(value) -> int:
    if not isinstance(value, str) or value.strip().lstrip("-").isdigit():
        return F._to_int(value)
    parsed = None
    with contextlib.suppress(ValueError, OverflowError):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    return 0


def _ms_to_iso(value: int) -> str | None:
    if not value:
        return None
    text = None
    with contextlib.suppress(ValueError, OverflowError, OSError):
        text = (
            datetime.fromtimestamp(value / 1000, UTC)
            .isoformat(
                timespec="milliseconds",
            )
            .replace("+00:00", "Z")
        )
    return text


def normalize_fornax_span(span: dict) -> dict:
    """Fornax 毫秒时间、名称、父 ID、自定义标签 → flux-insight span。"""
    tags = dict(span.get("custom_tags") or {})
    start = _time_ms(span.get("started_at"))
    duration = F._to_int(span.get("duration"))
    name = str(span.get("span_name") or "")
    parent = str(span.get("parent_id") or "")
    if parent and not parent.strip("0"):
        parent = ""
    mapped = {
        "span_name",
        "parent_id",
        "started_at",
        "duration",
        "custom_tags",
        "span_id",
        "trace_id",
        "span_type",
        "status",
        "input",
        "output",
    }
    extras = {key: value for key, value in span.items() if key not in mapped}
    extras["span_type"] = span.get("span_type")
    if "fornax_fields" in tags:
        extras["custom_tag_fornax_fields"] = tags["fornax_fields"]
    tags["fornax_fields"] = extras
    kind = name.split("(", 1)[0]
    if kind not in ("agent-turn", "agent-run", "model-call", "tool-call"):
        kind = span.get("span_type") or ""
    result = {
        "span_id": str(span.get("span_id") or ""),
        "trace_id": str(span.get("trace_id") or ""),
        "parent_span_id": parent or None,
        "name": name,
        "span_type": kind,
        "outcome": span.get("status"),
        "started_at": _ms_to_iso(start),
        "ended_at": _ms_to_iso(start + duration) if start else None,
        "duration_ms": duration,
        "input": span.get("input"),
        "output": span.get("output"),
        "attributes": tags,
    }
    usage = {
        key: F._to_int(tags.get(key))
        for key in ("input_tokens", "output_tokens", "reasoning_tokens")
        if key in tags
    }
    if "input_cached_tokens" in tags or "cached_tokens" in tags:
        usage["input_cached_tokens"] = F._first_int(
            tags,
            "input_cached_tokens",
            "cached_tokens",
        )
    if usage:
        result["usage"] = usage
    for key in (
        "task_id",
        "flux_session_id",
        "projection_session_id",
        "projection_session_role",
        "root_turn_id",
        "turn_id",
        "message_id",
        "service_psm",
        "log_id",
        "agent_type",
        "work_mode",
        "configured_model",
        "configured_thinking_effort",
        "task_source",
        "source_platform",
        "user_id",
    ):
        if key in span or key in tags:
            result[key] = span.get(key, tags.get(key))
    return result


def _iso_sort_key(span: dict) -> tuple:
    return (
        str(span.get("trace_id") or ""),
        _time_ms(span.get("started_at")),
        str(
            span.get("span_id") or "",
        ),
    )


def _span_graph(spans: list[dict]) -> tuple[dict, dict, list, int]:
    nodes = {_span_key(span): span for span in spans}
    by_id: dict[str, list[tuple]] = defaultdict(list)
    for key in nodes:
        by_id[key[1]].append(key)
    children: dict[tuple, list[tuple]] = defaultdict(list)
    roots = []
    dangling = 0
    for key, span in nodes.items():
        parent_id = str(span.get("parent_span_id") or "")
        parent = (key[0], parent_id)
        if parent not in nodes and len(by_id.get(parent_id, [])) == 1:
            parent = by_id[parent_id][0]
        if parent_id and parent in nodes:
            children[parent].append(key)
        else:
            roots.append(key)
            dangling += bool(parent_id)

    def order(key: tuple) -> tuple:
        return _time_ms(nodes[key].get("started_at")), key

    roots.sort(key=order)
    for keys in children.values():
        keys.sort(key=order)
    return nodes, children, roots, dangling


def render_tree(spans: list[dict]) -> str:
    """按 parent_span_id 组树；跨 trace 父子关联、深树和循环都不丢节点。"""
    nodes, children, roots, _ = _span_graph(spans)
    visited: set[tuple] = set()
    root_set = set(roots)
    lines: list[str] = []
    for root in roots + sorted(nodes):
        if root in visited:
            continue
        if root not in root_set:
            lines.append("[无明确根 / 循环关联]")
        stack = [(root, 0)]
        while stack:
            key, depth = stack.pop()
            if key in visited:
                continue
            visited.add(key)
            span = nodes[key]
            usage = span.get("usage") or {}
            label = (
                f"{span.get('name') or '?'} {F._to_int(span.get('duration_ms'))}ms"
                f" span={key[1]} trace={key[0]}"
            )
            if span.get("name") == "model-call":
                label += (
                    f" in={F._to_int(usage.get('input_tokens'))}"
                    f"/out={F._to_int(usage.get('output_tokens'))}"
                )
            outcome = span.get("outcome", span.get("status"))
            if outcome not in (None, "", "success"):
                label += f" outcome={outcome}"
            lines.append("  " * min(depth, 80) + ("└─ " if depth else "") + label)
            stack.extend(
                (child, depth + 1) for child in reversed(children.get(key, []))
            )
    return "\n".join(lines)


def summarize_spans(spans: list[dict]) -> dict:
    _, _, roots, dangling = _span_graph(spans)
    calls = [span for span in spans if span.get("name") == "model-call"]
    return {
        "span_count": len(spans),
        "trace_count": len({span.get("trace_id") for span in spans}),
        "model_call_count": len(calls),
        "input_tokens": sum(
            F._to_int((span.get("usage") or {}).get("input_tokens")) for span in calls
        ),
        "output_tokens": sum(
            F._to_int((span.get("usage") or {}).get("output_tokens")) for span in calls
        ),
        "root_count": len(roots),
        "dangling_count": dangling,
    }
