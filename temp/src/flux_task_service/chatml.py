"""Render exported conversation traces as readable ChatML text."""

import argparse
import json
import sys
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from flux_task_service.export_contract import ContractError, TaskDataReader, read_json

OutputMode = Literal["all", "user", "assistant"]
MessageRole = Literal["system", "developer", "user", "assistant", "tool", "function"]


@dataclass
class Message:
    role: str
    content: str
    session: str = "main"
    channel: str | None = None
    name: str | None = None
    recipient: str | None = None
    call_id: str | None = None
    timestamp: float = 0


@dataclass
class Transcript:
    messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    scopes: list[dict] = field(default_factory=list)

    def selected_messages(
        self,
        mode: OutputMode | Collection[OutputMode] = "all",
        *,
        exclude_roles: Collection[MessageRole] = (),
    ) -> list[Message]:
        """Select chronological messages; assistant mode keeps textual replies.

        All sessions are included. Assistant reasoning and tool calls are excluded
        from assistant mode, while commentary and final replies are retained.
        """
        modes = {mode} if isinstance(mode, str) else set(mode)
        if not modes or not modes <= {"all", "user", "assistant"}:
            raise ValueError(f"Unsupported output mode: {mode!r}")
        excluded = set(exclude_roles)
        if not excluded <= {
            "system",
            "developer",
            "user",
            "assistant",
            "tool",
            "function",
        }:
            raise ValueError(f"Unsupported excluded roles: {exclude_roles!r}")
        messages = [
            message
            for message in self.messages
            if message.role not in excluded
            and (
                "all" in modes
                or ("user" in modes and message.role == "user")
                or (
                    "assistant" in modes
                    and message.role == "assistant"
                    and message.channel != "analysis"
                    and message.recipient is None
                )
            )
        ]
        # Stable ordering preserves source order when timestamps are equal/unknown.
        return sorted(messages, key=lambda item: item.timestamp)

    def render(
        self,
        *,
        mode: OutputMode | Collection[OutputMode] = "all",
        exclude_roles: Collection[MessageRole] = (),
        visible_escapes: bool = False,
    ) -> str:
        """Render all messages, user prompts, or assistant replies as ChatML."""
        blocks = []
        for message in self.selected_messages(mode, exclude_roles=exclude_roles):
            header = message.role
            for key, value in (
                ("session", message.session),
                ("channel", message.channel),
                ("name", message.name),
                ("to", message.recipient),
                ("call_id", message.call_id),
            ):
                if value is not None:
                    header += f" {key}={json.dumps(str(value), ensure_ascii=False)}"
            body = message.content
            if visible_escapes:
                body = json.dumps(body, ensure_ascii=False)[1:-1]
            blocks.append(f"<|im_start|>{header}\n{body}\n<|im_end|>\n")
        return "".join(blocks)


def readable(value: Any) -> str:
    """Print structured values while leaving string newlines and Unicode intact."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        lines = []
        for key, child in value.items():
            if key in {"arguments", "args", "input", "output", "result"}:
                child = _json_body(child)
            text = readable(child)
            if isinstance(child, (dict, list)) or "\n" in text:
                lines.append(f"{key}:\n" + _indent(text, 2))
            else:
                lines.append(f"{key}: {text}")
        return "\n".join(lines) if lines else "{}"
    if isinstance(value, list):
        return (
            "\n".join("- " + readable(child).replace("\n", "\n  ") for child in value)
            if value
            else "[]"
        )
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _indent(text: str, width: int) -> str:
    return "\n".join(" " * width + line for line in text.split("\n"))


def _json_body(value: Any) -> Any:
    """Decode known JSON body fields once, never apply unicode_escape to prose."""
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            pass
    return value


def _time(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1000 if value > 100_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0


class TraceParser:
    def __init__(self):
        self.transcript = Transcript()
        self.seen: set[str] = set()
        self.projected: set[str] = set()
        self.sessions: dict[str, str] = {}

    def add(
        self, role: str, body: Any, session: str, timestamp: float, **headers: Any
    ) -> None:
        if body is not None:
            self.transcript.messages.append(
                Message(role, readable(body), session, timestamp=timestamp, **headers)
            )

    def step(self, step: dict, session: str, inherited_time: float = 0) -> None:
        identity = step.get("id")
        if identity:
            identity = str(identity)
            if identity in self.seen:
                return
            self.seen.add(identity)
        for key in (identity, step.get("contentOf")):
            if key:
                self.projected.add(str(key))
        native = (step.get("native") or {}).get("span", {})
        if native.get("projection_session_id"):
            self.sessions[str(native["projection_session_id"])] = session
        kind = step.get("kind")
        timestamp = _time(step.get("timestamp")) or inherited_time
        payload = step.get("payload") or {}
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise ValueError(f"Step {identity}: payload.data must be an object")
        if kind in {
            "user_message",
            "assistant_message",
            "agent_narration",
            "context_message",
            "system_note",
        }:
            role = data.get("role") or (
                "user"
                if kind == "user_message"
                else "system"
                if kind == "system_note"
                else "assistant"
            )
            self.add(
                role,
                data.get("text", data.get("content", data)),
                session,
                timestamp,
                channel="commentary" if kind == "agent_narration" else None,
            )
        elif kind == "reasoning":
            summary = data.get("summary", data.get("text", data.get("content")))
            if isinstance(summary, list):
                summary = "\n".join(
                    readable(item.get("text", item))
                    if isinstance(item, dict)
                    else readable(item)
                    for item in summary
                )
            self.add("assistant", summary, session, timestamp, channel="analysis")
            if data.get("has_encrypted_content"):
                self.transcript.warnings.append(
                    f"{identity}: encrypted reasoning is unavailable as text"
                )
        elif kind in {"tool_call", "skill", "mcp", "sub_agent"}:
            result = step.get("result") or {}
            native = (step.get("native") or {}).get("span", {})
            attributes = native.get("attributes") or {}
            call_id = (
                data.get("call_id")
                or result.get("call_id")
                or attributes.get("tool_call_id")
                or attributes.get("dispatch_id")
                or str(identity or "")
            )
            name = (
                data.get("name")
                or data.get("skill")
                or (
                    f"{data['server']}.{data['tool']}"
                    if data.get("server") and data.get("tool")
                    else step.get("title") or kind
                )
            )
            args = data.get("parsed_arguments", data.get("arguments", data.get("args")))
            if kind == "sub_agent":
                args = {
                    key: data[key] for key in ("description", "prompt") if key in data
                }
            self.add(
                "assistant",
                _json_body(args) if args is not None else data,
                session,
                timestamp,
                recipient=str(name),
                call_id=str(call_id),
            )
            output = result.get("output", data.get("reply"))
            if output is not None:
                ended = _time(native.get("ended_at"))
                if not ended:
                    ended = timestamp + (step.get("durationMs") or 0) / 1000
                self.add(
                    "tool",
                    _json_body(output),
                    session,
                    ended,
                    name=str(name),
                    call_id=str(call_id),
                )
            else:
                self.transcript.warnings.append(f"{identity}: tool result not recorded")
            for inner in data.get("innerSteps") or []:
                child = inner.get("step")
                if isinstance(child, dict):
                    self.step(child, str(call_id), timestamp)
        elif kind == "model":
            # Projected messages appear alongside model metadata. The metadata is
            # not another assistant utterance and must not duplicate its output.
            return
        else:
            self.transcript.warnings.append(
                f"{identity}: unrecognized step kind {kind}"
            )
            self.add(
                "system", {"trace_step_kind": kind, "payload": data}, session, timestamp
            )

    def message(self, message: dict, session: str, timestamp: float = 0) -> None:
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool", "function"}:
            raise ValueError(f"Unsupported message role: {role!r}")
        if message.get("reasoning_content"):
            self.add(
                "assistant",
                message["reasoning_content"],
                session,
                timestamp,
                channel="analysis",
            )
        if "content" in message and message["content"] is not None:
            content = message["content"]
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", readable(part))
                    if isinstance(part, dict)
                    else readable(part)
                    for part in content
                )
            self.add(
                role,
                content,
                session,
                timestamp,
                name=message.get("name"),
                call_id=message.get("tool_call_id"),
            )
        for call in message.get("tool_calls") or []:
            function = call.get("function", call)
            self.add(
                "assistant",
                _json_body(function.get("arguments", {})),
                session,
                timestamp,
                recipient=function.get("name"),
                call_id=call.get("id"),
            )

    def raw_spans(self, payload: dict) -> None:
        """Recover recorded bodies absent from projected steps, without duplication."""
        native_spans = payload.get("nativeSpans")
        if not isinstance(native_spans, list):
            native_spans = [
                {
                    "span": span,
                    "key": json.dumps(
                        [
                            span.get("flux_session_id"),
                            span.get("trace_id"),
                            span.get("span_id"),
                        ],
                        separators=(",", ":"),
                    ),
                }
                for span in payload.get("raw", [])
            ]
        omitted_inputs = 0
        missing_outputs = 0
        for native in native_spans:
            span = native.get("span") or {}
            key = str(native.get("key") or span.get("id") or "")
            kind = span.get("span_type")
            if kind not in {"model-call", "tool-call"}:
                continue
            attributes = span.get("attributes") or {}
            if kind == "model-call" and attributes.get("insight_input_omitted"):
                omitted_inputs += 1
            if key in self.projected or key in self.seen:
                continue
            self.seen.add(key)
            session_id = str(span.get("projection_session_id") or "main")
            session = self.sessions.get(session_id, session_id)
            timestamp = _time(span.get("started_at"))
            if kind == "tool-call":
                call_id = attributes.get("tool_call_id") or key
                name = attributes.get("tool_name") or span.get("name") or "tool"
                if "input" in span:
                    self.add(
                        "assistant",
                        _json_body(span["input"]),
                        session,
                        timestamp,
                        recipient=name,
                        call_id=call_id,
                    )
                if "output" in span:
                    self.add(
                        "tool",
                        _json_body(span["output"]),
                        session,
                        _time(span.get("ended_at")) or timestamp,
                        name=name,
                        call_id=call_id,
                    )
                else:
                    missing_outputs += 1
            elif "output" in span:
                output = _json_body(span["output"])
                if isinstance(output, dict) and isinstance(output.get("choices"), list):
                    for choice in output["choices"]:
                        message = choice.get("message")
                        if isinstance(message, dict):
                            self.message(message, session, timestamp)
                        else:
                            self.add("assistant", choice, session, timestamp)
                elif isinstance(output, dict) and isinstance(
                    output.get("content"), list
                ):
                    for block in output["content"]:
                        block_type = block.get("type")
                        if block_type == "text":
                            self.add("assistant", block.get("text"), session, timestamp)
                        elif block_type == "thinking":
                            self.add(
                                "assistant",
                                block.get("thinking"),
                                session,
                                timestamp,
                                channel="analysis",
                            )
                        elif block_type == "tool_use":
                            self.add(
                                "assistant",
                                block.get("input"),
                                session,
                                timestamp,
                                recipient=block.get("name"),
                                call_id=block.get("id"),
                            )
                        else:
                            self.add("assistant", block, session, timestamp)
                else:
                    self.add("assistant", output, session, timestamp)
            else:
                missing_outputs += 1
        if omitted_inputs:
            self.transcript.warnings.append(
                f"{omitted_inputs} model request inputs were omitted upstream; "
                "this transcript cannot reconstruct their original prompts"
            )
        if missing_outputs:
            self.transcript.warnings.append(
                f"{missing_outputs} unprojected spans have no recorded output"
            )

    def parse(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Conversation payload must be an object")
        if isinstance(payload.get("trace_detail"), dict):
            detail = payload["trace_detail"]
            meta = detail.get("meta") or {}
            if meta.get("complete") is not True:
                self.transcript.warnings.append("Source does not confirm full coverage")
            self.transcript.scopes.append(meta)
            self.parse(detail.get("data", detail))
        elif isinstance(payload.get("data"), dict):
            self.parse(payload["data"])
        elif isinstance(payload.get("turns"), list):
            segment = payload.get("segment") or {}
            session = str(segment.get("sessionId") or "main")
            for turn in payload["turns"]:
                for step in turn.get("steps") or []:
                    self.step(step, session, _time(turn.get("startedAt")))
            for dispatch, items in (payload.get("dispatchedSubAgents") or {}).items():
                for item in items:
                    if isinstance(item.get("step"), dict):
                        self.step(
                            item["step"], str(dispatch), _time(item.get("timestamp"))
                        )
            self.raw_spans(payload)
        elif isinstance(payload.get("messages"), list):
            for message in payload["messages"]:
                self.message(
                    message,
                    str(payload.get("session_id") or "main"),
                    _time(message.get("timestamp")),
                )
        elif isinstance(payload.get("steps"), list):
            for step in payload["steps"]:
                self.step(step, str(payload.get("session_id") or "main"))
        elif isinstance(payload.get("raw"), list) or isinstance(
            payload.get("nativeSpans"), list
        ):
            self.raw_spans(payload)
        else:
            raise ValueError(
                "Unsupported trace body: expected turns, steps or messages"
            )


def load_trace(path: str | Path, *, allow_legacy: bool = False) -> Transcript:
    """Read a task directory via its contract or an explicit full-trace JSON file."""
    path = Path(path)
    parser = TraceParser()
    if path.is_dir():
        reader = TaskDataReader(path, allow_legacy=allow_legacy)
        entry = reader.read_dataset("conversation")
        if entry["status"] not in {"available", "partial"}:
            raise ValueError(f"Conversation is {entry['status']}: {entry['errors']}")
        parser.transcript.scopes.append(entry["scope"])
        if entry["complete"] is not True:
            parser.transcript.warnings.append(
                "Conversation completeness is not confirmed"
            )
        for document in entry["documents"]:
            ref = document["reference"]
            if ref["role"] == "summary" or ref.get("content_kind") == "inventory":
                continue
            if ref.get("content_kind") not in {
                "full-trace",
                "messages-only",
                "chunk",
                "messages",
            }:
                raise ValueError(
                    "Conversation reference is missing a supported content_kind"
                )
            raw = document["content"]
            if ref["content_kind"] in {"full-trace", "messages-only"} and not raw.get(
                "trace_detail"
            ):
                continue
            parser.parse(raw)
    else:
        parser.parse(read_json(path))
    if not parser.transcript.messages:
        raise ValueError("No readable messages were found in this trace")
    parser.transcript.warnings = list(dict.fromkeys(parser.transcript.warnings))
    return parser.transcript


def main() -> int:
    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument(
        "input", type=Path, help="Task directory or full trace JSON"
    )
    argument_parser.add_argument(
        "-o", "--output", type=Path, help="UTF-8 text output; defaults to stdout"
    )
    argument_parser.add_argument("--allow-legacy", action="store_true")
    argument_parser.add_argument(
        "--mode",
        choices=("all", "user", "assistant"),
        action="append",
        help="Output all messages (default), user prompts, or assistant replies "
        "without reasoning and tool calls; repeat to combine modes",
    )
    argument_parser.add_argument(
        "--exclude-role",
        choices=("system", "developer", "user", "assistant", "tool", "function"),
        action="append",
        default=[],
        help="Exclude a message role; repeat to exclude multiple roles",
    )
    argument_parser.add_argument(
        "--visible-escapes",
        action="store_true",
        help="Show body control characters as literal \\n, \\t, etc.",
    )
    args = argument_parser.parse_args()
    modes = args.mode or ["all"]
    try:
        transcript = load_trace(args.input, allow_legacy=args.allow_legacy)
        text = transcript.render(
            mode=modes,
            exclude_roles=args.exclude_role,
            visible_escapes=args.visible_escapes,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        print(
            json.dumps(
                {
                    "messages": len(
                        transcript.selected_messages(
                            modes, exclude_roles=args.exclude_role
                        )
                    ),
                    "source_messages": len(transcript.messages),
                    "mode": modes[0] if len(modes) == 1 else modes,
                    "exclude_roles": args.exclude_role,
                    "warnings": transcript.warnings,
                    "scope": transcript.scopes,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    except (ContractError, ValueError, OSError) as exc:
        print(f"Trace conversion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
