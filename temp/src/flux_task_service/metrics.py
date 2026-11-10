"""Metric calculations migrated from the sibling Flux fetch scripts."""

import re
from datetime import UTC, datetime

from flux_task_service.token_usage import token_int

INT_RE = re.compile(r"-?\d+")


def _to_int(value) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and INT_RE.fullmatch(value.strip()):
        return int(value.strip())
    return 0


def _first_int(mapping: dict, *keys: str) -> int:
    """按顺序取第一个能转成正整数的字段（同一口径的多套字段名）。"""
    for key in keys:
        value = _to_int(mapping.get(key))
        if value:
            return value
    return 0


def _first_time(mapping: dict, *keys: str) -> int:
    """按顺序取第一个非零时间字段；ISO 字符串和毫秒整数都认。"""
    for key in keys:
        value = mapping.get(key)
        ms = _iso_to_ms(value) or _to_int(value)
        if ms:
            return ms
    return 0


def _iso_to_ms(value) -> int:
    """Parse ISO timestamps including offsets without inventing missing times."""
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


def _norm_fornax(
    model_calls: list[dict],
    turns: list[dict],
) -> tuple[list[dict], list[tuple[int, int]], dict]:
    """Fornax span -> 统一结构。"""
    calls: list[dict] = []
    for span in model_calls:
        tags = span.get("custom_tags") or {}
        calls.append(
            {
                "started_at": _to_int(span.get("started_at")),
                "input_tokens": _to_int(tags.get("input_tokens")),
                "output_tokens": _to_int(tags.get("output_tokens")),
                "reasoning_tokens": _to_int(tags.get("reasoning_tokens")),
                "cached_tokens": _first_int(
                    tags, "input_cached_tokens", "cached_tokens"
                ),
                "first_token_latency_ms": token_int(tags.get("first_token_latency_ms")),
            }
        )

    intervals: list[tuple[int, int]] = []
    primary_turns = 0
    for span in turns:
        tags = span.get("custom_tags") or {}
        role = tags.get("projection_session_role") or ""
        name = span.get("span_name") or ""
        if role and role != "primary":
            continue
        if not role and not name.startswith("agent-turn(primary)"):
            continue
        primary_turns += 1
        start = _to_int(span.get("started_at"))
        duration = token_int(span.get("duration"))
        if start <= 0 or duration is None:
            continue
        intervals.append((start, start + duration))
    return (
        calls,
        intervals,
        {
            "known_primary_turns": len(intervals),
            "primary_turns": primary_turns,
            "counts_consistent": True,
        },
    )


def _norm_insight_openapi(
    data: dict,
) -> tuple[list[dict], list[tuple[int, int]], int, int | None, str, dict]:
    """Use summary Token usage and primary turn timing from the read-model."""
    payload = data.get("data") or {}
    summary = payload.get("summary") or {}
    usage = summary.get("token_usage") or {}

    calls: list[dict] = []
    if usage:
        calls.append(
            {
                "started_at": 0,
                "input_tokens": _first_int(usage, "input", "input_tokens"),
                "output_tokens": _first_int(usage, "output", "output_tokens"),
                "reasoning_tokens": _first_int(
                    usage, "reasoning_output", "reasoning_tokens"
                ),
                "cached_tokens": _first_int(
                    usage,
                    "cached_input",
                    "input_cached_tokens",
                    "cached_tokens",
                ),
                "first_token_latency_ms": None,
            }
        )

    intervals: list[tuple[int, int]] = []
    turn_timings: list[tuple[int, dict]] = []
    mc_timings: list[tuple[int, int | None]] = []
    primary_turns = 0
    observed_turns = 0
    observed_model_calls = 0
    primary_starts_known = True
    counts_consistent = True
    for session in payload.get("sessions") or []:
        role = str(session.get("role") or "").lower()
        is_primary = True
        if role:
            is_primary = "primary" in role or "main" in role
        elif session.get("parent_session_id"):
            is_primary = False
        turns = session.get("turns") or []
        observed_turns += len(turns)
        if is_primary:
            expected = token_int((session.get("summary") or {}).get("turns"))
            primary_turns += max(len(turns), expected or 0)
            if expected is not None and expected != len(turns):
                counts_consistent = False
        for turn in turns:
            for call in turn.get("model_calls") or []:
                observed_model_calls += 1
                mc_start = _first_time(call, "started_at")
                if mc_start > 0:
                    mc_timings.append((mc_start, token_int(call.get("ttft_ms"))))
            if not is_primary:
                continue
            timing = turn.get("timing") or {}
            start = _first_time(timing, "started_at")
            if start <= 0:
                primary_starts_known = False
                continue
            turn_timings.append((start, timing))
            end = _first_time(timing, "ended_at")
            if not end:
                duration = token_int(timing.get("duration_ms"))
                if duration is None:
                    continue
                end = start + duration
            if end < start:
                continue
            intervals.append((start, end))

    expected_turns = token_int(
        ((payload.get("meta") or {}).get("counts") or {}).get("turns")
    )
    if expected_turns is not None and expected_turns != observed_turns:
        counts_consistent = False
    coverage = {
        "known_primary_turns": len(intervals),
        "primary_turns": primary_turns,
        "counts_consistent": counts_consistent,
    }

    count = token_int(
        (((payload.get("meta") or {}).get("counts")) or {}).get("model_calls")
    )
    if count is None:
        counts = [
            token_int(model.get("calls")) for model in summary.get("models") or []
        ]
        if counts and all(value is not None for value in counts):
            count = sum(counts)
        elif observed_model_calls:
            count = observed_model_calls

    # 首响口径与 Fornax/flux-insight 一致：最早一个 model-call 的 ttft_ms
    first_resp = None
    if mc_timings and len(mc_timings) == observed_model_calls:
        first_resp = min(mc_timings, key=lambda item: item[0])[1]
    elif not observed_model_calls and turn_timings and primary_starts_known:
        first_resp = token_int(
            min(turn_timings, key=lambda item: item[0])[1].get("ttft_ms")
        )

    if not usage and not intervals:
        return [], [], 0, None, "trace 里没有 token_usage / turn", coverage
    return calls, intervals, count, first_resp, "", coverage


def apply_timing(result: dict, coverage: dict) -> None:
    """Require coverage of primary Turns and a known first-response latency."""
    turn_complete = (
        bool(coverage["primary_turns"])
        and coverage["known_primary_turns"] == coverage["primary_turns"]
        and coverage["counts_consistent"]
    )
    first_available = result["values"]["latency_ms_first_resp"] is not None
    source_complete = result["execution_complete"]
    result["timing_coverage"] = coverage
    result["timing_source"] = result["source"]
    result["total_latency_available"] = result["values"]["total_latency_ms"] is not None
    result["total_latency_complete"] = source_complete if turn_complete else False
    result["first_response_complete"] = (
        source_complete if first_available and turn_complete else False
    )
    result["first_response_available"] = first_available
    result["turn_timing_complete"] = source_complete if turn_complete else False
    result["timing_complete"] = (
        source_complete if turn_complete and first_available else False
    )
    result["timing_diagnostics"] = []
    if not turn_complete:
        result["timing_diagnostics"].append("PRIMARY_TURN_TIMING_INCOMPLETE")
    if not coverage["counts_consistent"]:
        result["timing_diagnostics"].append("TURN_COUNT_MISMATCH")
    if not first_available:
        result["timing_diagnostics"].append("FIRST_RESPONSE_LATENCY_UNAVAILABLE")
    if source_complete is not True:
        result["timing_diagnostics"].append("TIMING_SOURCE_COMPLETENESS_UNCONFIRMED")


def _latency(intervals: list[tuple[int, int]]) -> tuple[int, int, int]:
    """返回 (end_to_end, total(并集), interaction)。"""
    if not intervals:
        return 0, 0, 0
    end_to_end = max(end for _, end in intervals) - min(start for start, _ in intervals)

    ordered = sorted(intervals)
    cur_start, cur_end = ordered[0]
    total = 0
    for start, end in ordered[1:]:
        if start <= cur_end:
            if end > cur_end:
                cur_end = end
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start

    interaction = end_to_end - total
    if interaction < 0:
        interaction = 0
    return end_to_end, total, interaction


def build_metrics(
    model_calls: list[dict],
    intervals: list[tuple[int, int]],
    *,
    model_call_count: int | None = None,
    first_resp: int | None = None,
) -> dict:
    """Aggregate call usage and the union of primary turn time intervals."""
    input_tokens = sum(call["input_tokens"] for call in model_calls)
    output_tokens = sum(call["output_tokens"] for call in model_calls)
    reasoning_tokens = sum(call["reasoning_tokens"] for call in model_calls)
    cached_tokens = sum(call["cached_tokens"] for call in model_calls)
    clean_total_token = (
        input_tokens - cached_tokens + output_tokens
        if input_tokens >= cached_tokens
        else None
    )

    first_resp_value = first_resp
    if first_resp is None and model_calls:
        if all(call["started_at"] > 0 for call in model_calls):
            first_call = min(model_calls, key=lambda call: call["started_at"])
            first_resp_value = token_int(first_call.get("first_token_latency_ms"))

    count = model_call_count if model_call_count is not None else len(model_calls)
    end_to_end, total, interaction = _latency(intervals)
    if not intervals:
        end_to_end = total = interaction = None
    return {
        "total_tokens": input_tokens + output_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "clean_total_token": clean_total_token,
        "end_to_end_latency_ms": end_to_end,
        "total_latency_ms": total,
        "interaction_latency_ms": interaction,
        "latency_ms_first_resp": first_resp_value,
        "model_call_count": count,
    }
