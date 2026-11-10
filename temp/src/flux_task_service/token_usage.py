"""Normalize Token totals without treating missing values as zero."""

TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cached_tokens": "cached_input",
    "reasoning_tokens": "reasoning_output",
    "cache_creation_input_tokens": "cache_creation_input",
    "non_cached_input_tokens": "non_cached_input",
}


def token_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if (
        isinstance(value, str)
        and 1 <= len(value.strip()) <= 64
        and value.strip().isascii()
        and value.strip().isdigit()
    ):
        return int(value.strip())
    return None


def normalize_tokens(
    usage: dict,
    *,
    source: str,
    known: bool | None,
    complete: bool | None,
    coverage: dict | None = None,
    ambiguous_input: bool = False,
) -> dict:
    known = known if isinstance(known, bool) else None
    complete = complete if isinstance(complete, bool) else None
    if known is None and complete is not True:
        if not any(
            (token_int(usage.get(field)) or 0) > 0 for field in TOKEN_FIELDS.values()
        ):
            known = False
    values = {key: None for key in TOKEN_FIELDS}
    values.update(total_tokens=None, clean_total_token=None)
    diagnostics = []
    if known is not False:
        values.update(
            {key: token_int(usage.get(field)) for key, field in TOKEN_FIELDS.items()}
        )
        input_count = values["input_tokens"]
        cached = values["cached_tokens"]
        output = values["output_tokens"]
        if ambiguous_input or (
            input_count is not None and cached is not None and input_count < cached
        ):
            input_count = None
            values["input_tokens"] = None
            values["non_cached_input_tokens"] = None
            complete = False
            diagnostics.append("TOKEN_INPUT_BASIS_UNCONFIRMED")
        if input_count is not None and output is not None:
            total = input_count + output
            reported_total = token_int(usage.get("total"))
            if reported_total is not None and reported_total != total:
                complete = False
                diagnostics.append("TOKEN_TOTAL_INCONSISTENT")
            else:
                values["total_tokens"] = total
        else:
            complete = False
        if values["non_cached_input_tokens"] is None:
            if input_count is not None and cached is not None:
                values["non_cached_input_tokens"] = input_count - cached
        non_cached = values["non_cached_input_tokens"]
        if non_cached is not None and output is not None:
            values["clean_total_token"] = non_cached + output
    else:
        complete = False
        diagnostics.append("TOKEN_USAGE_UNAVAILABLE")
    coverage = coverage or {"known_model_calls": None, "model_calls": None}
    if (
        coverage.get("known_model_calls") is not None
        and coverage.get("model_calls") is not None
        and coverage["known_model_calls"] < coverage["model_calls"]
    ):
        complete = False
        diagnostics.append("TOKEN_CALL_COVERAGE_INCOMPLETE")
    available = any(value is not None for value in values.values())
    if complete is not True:
        diagnostics.append("TOKEN_COMPLETENESS_UNCONFIRMED")
    return {
        "values": values,
        "tokens_available": available,
        "tokens_complete": complete,
        "token_source": source,
        "token_coverage": coverage,
        "token_diagnostics": list(dict.fromkeys(diagnostics)),
        "token_evidence": {
            "source": source,
            "known": known,
            "complete": complete,
            "usage": {
                key: usage.get(key)
                for key in (*TOKEN_FIELDS.values(), "total", "total_source")
                if key in usage
            },
        },
    }


def summary_tokens(payload: dict) -> dict:
    usage = (payload.get("summary") or {}).get("token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    normalized = normalize_tokens(
        usage,
        source="trace_summary",
        known=usage.get("known"),
        complete=usage.get("total_known"),
        coverage={
            "known_model_calls": None,
            "model_calls": token_int(
                (payload.get("meta", {}).get("counts") or {}).get("model_calls")
            ),
        },
        ambiguous_input=usage.get("total_source") == "mixed",
    )

    return execution_scope(normalized, payload.get("meta") or {})


def execution_complete(meta: dict) -> bool | None:
    coverage = meta.get("coverage")
    if isinstance(coverage, list) and any(
        isinstance(entry, dict)
        and entry.get("required") is True
        and entry.get("status") in {"partial", "error", "missing"}
        for entry in coverage
    ):
        return False
    if meta.get("errors"):
        return False
    complete = meta.get("complete")
    return complete if isinstance(complete, bool) else None


def execution_scope(normalized: dict, meta: dict) -> dict:
    complete = execution_complete(meta)
    if complete is not True:
        if normalized["tokens_complete"] is not False:
            normalized["tokens_complete"] = False if complete is False else None
        normalized["token_diagnostics"].append(
            "TOKEN_EXECUTION_COMPLETENESS_UNCONFIRMED"
        )
    normalized["token_scope"] = {
        "kind": "task_trace",
        "execution_complete": complete,
        **{
            key: meta[key]
            for key in ("scope", "coverage", "omitted", "errors", "completenessScope")
            if key in meta
        },
    }
    return normalized


def view_tokens(data: dict, task_id: str) -> dict | None:
    """Read one canonical task total, never sum repeated nested usage objects."""
    detail = data.get("trace_detail")
    if not isinstance(detail, dict) or not isinstance(detail.get("data"), dict):
        return None
    view = detail["data"]
    meta = detail.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    scope = meta.get("scope") or view.get("scope") or {}
    if isinstance(scope, dict) and scope.get("taskId") not in (None, task_id):
        return None
    scene = view.get("data")
    if not isinstance(scene, dict):
        return None
    totals = scene.get("totals") or {}
    if not isinstance(totals, dict) or totals.get("tokenUsageKnown") is not True:
        return None
    usage = totals.get("tokenUsage")
    if not isinstance(usage, dict):
        return None
    raw_coverage = scene.get("mongoCoverage") or {}
    if not isinstance(raw_coverage, dict):
        raw_coverage = {}
    known = token_int(raw_coverage.get("knownModelCalls"))
    count = token_int(raw_coverage.get("modelCalls"))
    complete = None
    if known is not None and count is not None and 0 <= known <= count and count > 0:
        complete = known == count
    elif known is not None and count is not None and known > count:
        complete = False
    normalized = normalize_tokens(
        usage,
        source="trace_view_totals",
        known=True,
        complete=complete,
        coverage={"known_model_calls": known, "model_calls": count},
    )
    execution_scope(normalized, meta)
    return normalized if normalized["tokens_available"] else None


def apply_tokens(result: dict, normalized: dict) -> None:
    """Retain source snapshots and known values; never sum different source totals."""
    source = normalized["token_source"]
    sources = result.setdefault("token_sources", [])
    sources.append(normalized)
    first = "token_source" not in result
    existing = result["values"]
    incoming = normalized["values"]
    conflict = not first and any(
        existing.get(key) is not None and value is not None and existing[key] != value
        for key, value in incoming.items()
    )
    fields = result.setdefault("token_field_sources", {})
    used = False
    for key, value in incoming.items():
        # Conflicting totals cannot safely supplement a different accounting basis.
        if first or (not conflict and existing.get(key) is None and value is not None):
            existing[key] = value
            if value is not None:
                fields[key] = source
                used = True
    stronger = (
        normalized["tokens_complete"] is True
        and result.get("tokens_complete") is not True
    )
    had_conflict = "TOKEN_SOURCE_CONFLICT" in result.get("token_diagnostics", [])
    if first or ((used or stronger) and not conflict):
        result.update(
            {key: value for key, value in normalized.items() if key != "values"}
        )
    if conflict or had_conflict:
        result["tokens_complete"] = False
        result["token_diagnostics"] = list(
            dict.fromkeys(result["token_diagnostics"] + ["TOKEN_SOURCE_CONFLICT"])
        )
    if source == "trace_view_totals" and not conflict:
        count = normalized["token_coverage"].get("model_calls")
        if count is not None:
            existing["model_call_count"] = count
    if (
        result.get("token_scope", {}).get("kind") == "fornax_window"
        and result["source"] != "fornax"
    ):
        result["tokens_complete"] = False
        result["token_diagnostics"] = list(
            dict.fromkeys(result["token_diagnostics"] + ["TOKEN_TIMING_SCOPE_MISMATCH"])
        )
    execution = result.get("timing_complete", result.get("execution_complete"))
    tokens = result["tokens_complete"]
    result["complete"] = (
        False
        if execution is False or tokens is False
        else (True if execution is True and tokens is True else None)
    )
    result["warnings"] = [
        warning
        for warning in result["warnings"]
        if warning not in {"OpenAPI 标记 Token 统计不完整", "Token 统计未确认完整"}
    ]
    if tokens is not True:
        result["warnings"].append("Token 统计未确认完整")


def task_tokens(data: dict, task_id: str) -> dict | None:
    items = data.get("items")
    if not isinstance(items, list):
        return None
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("id_kind") == "task"
        and item.get("task_id") == task_id
    ]
    if len(matches) != 1 or not isinstance(matches[0].get("task"), dict):
        return None
    task = matches[0]["task"]
    usage = {field: task.get(key) for key, field in TOKEN_FIELDS.items()}
    usage["total"] = task.get("total_tokens")
    if not any((token_int(value) or 0) > 0 for value in usage.values()):
        return None
    normalized = normalize_tokens(
        usage, source="task_detail_totals", known=True, complete=None
    )
    # An explicit total alone is useful, but says nothing about individual calls.
    if normalized["values"]["total_tokens"] is None and not any(
        code in normalized["token_diagnostics"]
        for code in ("TOKEN_TOTAL_INCONSISTENT", "TOKEN_INPUT_BASIS_UNCONFIRMED")
    ):
        normalized["values"]["total_tokens"] = token_int(usage["total"])
    normalized["tokens_available"] = any(
        v is not None for v in normalized["values"].values()
    )
    normalized["tokens_complete"] = None
    normalized["token_scope"] = {"kind": "task_accumulated", "task_id": task_id}
    normalized["token_diagnostics"].append("TASK_TOTALS_NOT_TRACE_USAGE")
    return normalized


def fornax_tokens(calls: list[dict], complete: bool) -> dict:
    fields = {
        "input": ("input_tokens",),
        "output": ("output_tokens",),
        "cached_input": ("input_cached_tokens", "cached_tokens"),
        "reasoning_output": ("reasoning_tokens",),
    }
    usage = {}
    known_calls = 0
    for call in calls:
        tags = call.get("custom_tags")
        tags = tags if isinstance(tags, dict) else {}
        if all(
            token_int(tags.get(key)) is not None
            for key in ("input_tokens", "output_tokens")
        ):
            known_calls += 1
    for field, keys in fields.items():
        values = []
        for call in calls:
            tags = call.get("custom_tags")
            tags = tags if isinstance(tags, dict) else {}
            value = next(
                (
                    token_int(tags.get(key))
                    for key in keys
                    if token_int(tags.get(key)) is not None
                ),
                None,
            )
            if value is not None:
                values.append(value)
        usage[field] = sum(values) if values else None
    return normalize_tokens(
        usage,
        source="fornax_usage",
        known=True
        if known_calls > 0
        else (
            None
            if any(value is not None and value > 0 for value in usage.values())
            else False
        ),
        complete=complete and known_calls == len(calls) and bool(calls),
        coverage={"known_model_calls": known_calls, "model_calls": len(calls)},
    )
