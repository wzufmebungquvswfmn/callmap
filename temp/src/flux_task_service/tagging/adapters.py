"""Explicit source adapters; arbitrary text is never interpreted as an event."""

import hashlib
import json
import re
from dataclasses import replace
from typing import Any

from .models import Event, Evidence


def decoded(value: Any) -> Any:
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            break
    return value


def object_value(value: Any) -> dict:
    value = decoded(value)
    return value if isinstance(value, dict) else {}


def pointer_part(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _event(node: dict, evidence: Evidence, *, span=False) -> Event:
    if span:
        payload = object_value(node.get("input"))
        kind = node.get("span_type", node.get("name", "").split("(")[0])
        kind = {
            "tool-call": "tool_call",
            "agent-run": "agent_run",
            "model-call": "model_call",
            "agent-turn": "agent_turn",
        }.get(kind, kind)
        name = node.get("name", "")
        if not isinstance(name, str):
            raise ValueError("Execution name must be a string")
        match = re.fullmatch(r"tool-call\((.*)\)", name)
        if match:
            name = match[1]
        output = decoded(node.get("output"))
        status = node.get("outcome", node.get("status"))
        evidence = replace(
            evidence,
            trace_id=node.get("trace_id"),
            span_id=node.get("span_id"),
            session_id=node.get("projection_session_id") or node.get("flux_session_id"),
            turn_id=node.get("turn_id"),
        )
    else:
        kind = node.get("kind", "")
        payload = node.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("Step payload must be an object or null")
        payload = payload.get("data", payload)
        payload = payload if isinstance(payload, dict) else {}
        name = payload.get("name") or node.get("title", "").removeprefix("Tool Call: ")
        output = node.get("result") or {}
        status = node.get("status")
        relationship = node.get("evidence") or {}
        evidence = replace(
            evidence,
            trace_id=relationship.get("trace_id"),
            span_id=relationship.get("span_id"),
        )
    arguments = decoded(payload.get("parsed_arguments") or payload.get("arguments"))
    if arguments is None and any(k in payload for k in ("command", "cmd", "skill")):
        arguments = payload
    result = object_value(output)
    call_id = payload.get("call_id") or result.get("call_id")
    evidence = replace(
        evidence, event_id=str(node.get("id") or "") or None, call_id=call_id
    )
    code = result.get("exit_code")
    if type(code) is not int:
        code = None
    if not span:
        output = decoded(result.get("output", result.get("raw_output")))
    return Event(
        kind,
        name,
        arguments,
        output,
        status,
        evidence,
        code,
        result.get("truncated") is True,
    )


def _steps(steps: list, evidence: Evidence) -> list[Event]:
    if not isinstance(steps, list):
        raise ValueError("Steps must be an array")
    events = []
    stack = [
        (s, f"{evidence.pointer}/{i}") for i, s in reversed(list(enumerate(steps)))
    ]
    while stack:
        node, pointer = stack.pop()
        if not isinstance(node, dict):
            raise ValueError("Step must be an object")
        events.append(_event(node, replace(evidence, pointer=pointer)))
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(
                (s, f"{pointer}/children/{i}")
                for i, s in reversed(list(enumerate(children)))
            )
    return events


def execution_events(content: Any, fmt: str, evidence: Evidence) -> list[Event]:
    if fmt in {"flux.normalized-spans.v1", "json-records.v1"}:
        return [
            _event(s, replace(evidence, pointer=f"{evidence.pointer}/{i}"), span=True)
            for i, s in enumerate(content)
            if isinstance(s, dict)
        ]
    if fmt != "trace-read-model.v2":
        return []
    events = []
    for si, session in enumerate(content.get("sessions", [])):
        if not isinstance(session, dict) or not isinstance(
            session.get("turns", []), list
        ):
            raise ValueError("Session must contain a turns array")
        for ti, turn in enumerate(session.get("turns", [])):
            if not isinstance(turn, dict):
                raise ValueError("Turn must be an object")
            context = replace(
                evidence,
                session_id=session.get("id"),
                turn_id=turn.get("source_turn_id") or turn.get("id"),
                pointer=f"{evidence.pointer}/sessions/{si}/turns/{ti}/steps",
            )
            events.extend(_steps(turn.get("steps", []), context))
    return events


def conversation_events(
    raw: dict, kind: str | None, evidence: Evidence, *, include_projected: bool = False
) -> list[Event]:
    if kind in {"inventory", None}:
        return []
    if kind in {"full-trace", "messages-only"}:
        body = raw.get("trace_detail")
        if not isinstance(body, dict):
            return []
        evidence = replace(evidence, pointer=evidence.pointer + "/trace_detail")
    elif kind in {"chunk", "messages"}:
        body = raw
    else:
        return []
    # These are explicit transport wrappers, not arbitrary recursive object searches.
    for _ in range(3):
        if isinstance(body.get("data"), dict):
            body = body["data"]
            evidence = replace(evidence, pointer=evidence.pointer + "/data")
        else:
            break
    events = []
    if isinstance(body.get("raw"), list):
        events = execution_events(
            body["raw"],
            "json-records.v1",
            replace(evidence, pointer=evidence.pointer + "/raw"),
        )
        if not include_projected:
            return events
    for i, turn in enumerate(body.get("turns", [])):
        if isinstance(turn, dict):
            context = replace(
                evidence,
                turn_id=turn.get("sourceTurnId") or turn.get("id"),
                pointer=f"{evidence.pointer}/turns/{i}/steps",
            )
            events.extend(_steps(turn.get("steps", []), context))
    if isinstance(body.get("steps"), list):
        events.extend(
            _steps(
                body["steps"], replace(evidence, pointer=evidence.pointer + "/steps")
            )
        )
    return events


def event_key(event: Event) -> str:
    ref = event.evidence
    if ref.trace_id and ref.span_id:
        # Narration/reasoning may reference their model-call's span; those are
        # different semantic records, not duplicate model invocations.
        return f"span:{ref.trace_id}:{ref.span_id}:{event.kind}"
    if ref.call_id:
        return f"call:{ref.session_id or ''}:{ref.call_id}"
    return f"location:{ref.dataset}:{ref.path}:{ref.pointer}"


def deduplicate(events: list[Event]) -> list[Event]:
    """Merge duplicate views using IDs; preserve richer arguments/result evidence."""
    result: dict[str, Event] = {}
    calls: dict[tuple[str | None, str], str] = {}
    for event in events:
        key = event_key(event)
        call = event.evidence.call_id if event.kind == "tool_call" else None
        call_key = (event.evidence.session_id, call) if call else None
        # Call IDs can be reused by different sessions; spans already use trace IDs.
        if call_key and call_key in calls:
            key = calls[call_key]
        if call:
            calls[call_key] = key
        previous = result.get(key)
        if previous is None:
            result[key] = event
        elif event.arguments and not previous.arguments:
            result[key] = replace(event, output=event.output or previous.output)
        elif event.output and not previous.output:
            result[key] = replace(
                previous,
                output=event.output,
                exit_code=event.exit_code
                if event.exit_code is not None
                else previous.exit_code,
            )
    return list(result.values())


def stable_id(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:20]
