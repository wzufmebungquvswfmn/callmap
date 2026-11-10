"""Recover task raw spans using bounded session-scoped OpenAPI queries."""

import asyncio
import json

from flux_task_service.client import OpenAPIClient
from flux_task_service.errors import ServiceError


def span_references(payload: dict) -> set[str]:
    """Use task-scoped read-model evidence to recognize otherwise untagged spans."""
    references = set()
    stack = [payload.get("sessions", [])]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "span_id" and isinstance(child, str):
                    references.add(child)
                elif key == "source_span_ids" and isinstance(child, list):
                    references.update(item for item in child if isinstance(item, str))
                stack.append(child)
        elif isinstance(value, list):
            stack.extend(value)
    return references


async def recover_raw_spans(
    api: OpenAPIClient, task_id: str, payload: dict
) -> tuple[list[dict], dict]:
    """Never infer task ownership from a shared physical session alone."""
    settings = api.settings
    sessions = payload.get("sessions") or []
    session_ids = list(
        dict.fromkeys(
            session["id"]
            for session in sessions
            if isinstance(session, dict)
            and isinstance(session.get("id"), str)
            and session["id"].isascii()
            and session["id"].isdigit()
            and 1 <= len(session["id"]) <= 64
            and session["id"].strip("0")
        )
    )
    references = span_references(payload)
    expected = (payload.get("meta", {}).get("counts") or {}).get("spans")
    report = {
        "source": "flux_insight_openapi",
        "scope": "task-spans-from-physical-sessions",
        "status": "unavailable",
        "complete": False,
        "session_count": len(session_ids),
        "session_ids_are_candidates": True,
        "expected_spans": expected,
        "recovered_spans": 0,
        "bytes_read": 0,
        "sessions": [],
        "errors": [],
    }
    recovered = {}
    if not session_ids:
        report["errors"].append({"code": "RAW_SESSION_IDS_UNAVAILABLE"})
        return [], report
    if len(session_ids) == 1:
        # Querying the only session cannot split a large task into smaller
        # pieces, and may widen the scope to other tasks sharing that session.
        report["status"] = "not_attempted"
        report["errors"].append(
            {
                "code": "RAW_RECOVERY_NOT_SPLITTABLE",
                "message": "Only one session candidate; no smaller query scope",
            }
        )
        return [], report
    try:
        async with asyncio.timeout(settings.raw_recovery_timeout_seconds):
            for session_id in session_ids[: settings.raw_recovery_max_sessions]:
                remaining = settings.raw_recovery_max_bytes - report["bytes_read"]
                if remaining <= 0:
                    report["errors"].append({"code": "RAW_RECOVERY_BYTE_LIMIT"})
                    break
                record = {"session_id": session_id, "status": "error"}
                report["sessions"].append(record)
                try:
                    data = await api.request_json(
                        "/trace/query",
                        {
                            "worker_session_id": session_id,
                            "include_raw_spans": True,
                            "compress": True,
                        },
                        headers={"X-Flux-Trace-Contract": "2"},
                        max_retries=0,
                        max_response_bytes=remaining,
                    )
                    size = len(
                        json.dumps(
                            data, ensure_ascii=False, separators=(",", ":")
                        ).encode()
                    )
                    report["bytes_read"] += size
                    if report["bytes_read"] > settings.raw_recovery_max_bytes:
                        report["errors"].append({"code": "RAW_RECOVERY_BYTE_LIMIT"})
                        break
                    raw = data.get("raw_spans")
                    if not isinstance(raw, list) or any(
                        not isinstance(span, dict)
                        or not isinstance(span.get("span_id"), str)
                        or not span["span_id"]
                        or not isinstance(span.get("trace_id"), str)
                        or not span["trace_id"]
                        for span in raw
                    ):
                        raise ServiceError(
                            "UPSTREAM_PROTOCOL_ERROR", "Invalid session raw spans"
                        )
                    selected = []
                    for span in raw:
                        original = span.get("payload")
                        owner = (
                            original.get("task_id")
                            if isinstance(original, dict)
                            else None
                        )
                        if owner not in (None, ""):
                            belongs = str(owner) == task_id
                        else:
                            belongs = span["span_id"] in references
                        if belongs:
                            selected.append(span)
                    for span in selected:
                        recovered[(span["trace_id"], span["span_id"])] = span
                    meta = data.get("meta") or {}
                    complete = isinstance(meta, dict) and meta.get("complete") is True
                    record.update(
                        status="ok" if complete else "partial",
                        complete=complete,
                        received_spans=len(raw),
                        selected_spans=len(selected),
                        excluded_spans=len(raw) - len(selected),
                    )
                except ServiceError as exc:
                    record["error"] = {"code": exc.code}
                    if exc.upstream_code is not None:
                        record["error"]["upstream_code"] = exc.upstream_code
                    if exc.code == "DOWNLOAD_LIMIT_EXCEEDED":
                        # Its consumed size is unknown; do not spend a fresh byte
                        # budget on the next candidate after this bounded read.
                        report["errors"].append({"code": "RAW_RECOVERY_BYTE_LIMIT"})
                        break
                    # Credential/permission failures affect every remaining session.
                    if exc.code in {
                        "AUTH_NOT_CONFIGURED",
                        "IAM_UNAVAILABLE",
                        "IAM_AUTH_FAILED",
                        "UPSTREAM_UNAUTHORIZED",
                        "UPSTREAM_FORBIDDEN",
                    }:
                        report["errors"].append({"code": "RAW_RECOVERY_AUTH_FAILED"})
                        break
    except TimeoutError:
        report["errors"].append({"code": "RAW_RECOVERY_TIMEOUT"})
    if len(session_ids) > settings.raw_recovery_max_sessions:
        report["errors"].append({"code": "RAW_RECOVERY_SESSION_LIMIT"})
    report["recovered_spans"] = len(recovered)
    if expected != len(recovered):
        report["errors"].append({"code": "RAW_RECOVERY_COUNT_MISMATCH"})
    report["complete"] = (
        payload.get("meta", {}).get("complete") is True
        and isinstance(expected, int)
        and not isinstance(expected, bool)
        and expected > 0
        and len(recovered) == expected
        and references.issubset({span["span_id"] for span in recovered.values()})
        and len(report["sessions"]) == len(session_ids)
        and all(record["status"] == "ok" for record in report["sessions"])
        and not report["errors"]
    )
    report["status"] = (
        "complete" if report["complete"] else "partial" if recovered else "unavailable"
    )
    return list(recovered.values()), report
