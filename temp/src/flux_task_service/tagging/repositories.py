"""Conservative repository extraction with source-local, auditable evidence.

Source categories describe how an identity was found, never whether its code was
used. This offline extractor does not infer remote identities from local paths.
"""

import json
import re
from dataclasses import asdict
from urllib.parse import urlsplit

from .adapters import (
    conversation_events,
    decoded,
    deduplicate,
    object_value,
    pointer_part,
)
from .commands import command_segments
from .models import Event, Evidence

CATEGORY_LABELS = {
    "platform_field": "上游仓库字段",
    "user_specified": "用户指定",
    "runtime_context": "运行时上下文",
    "commit_metadata": "提交记录",
    "tool_execution": "工具执行",
}
PURPOSE_LABELS = {
    "target_code": "目标代码",
    "reference_code": "参考代码",
    "skill_knowledge": "Skill / 知识来源",
    "artifact_output": "文档 / 产物输出",
    "execution_context": "执行上下文",
    "unknown": "用途未知",
}
_PURPOSE_CUES = {
    "target_code": r"(?:目标|产品|业务|源)(?:代码)?仓库"
    r"|(?:修改|修复|开发|分析|审查|测试)(?:代码)?仓库"
    r"|target(?: code)? repo(?:sitory)?",
    "reference_code": r"参考(?:代码)?(?:仓库)?"
    r"|reference(?: code)?(?: repo(?:sitory)?)?",
    "skill_knowledge": r"skill(?:s)?(?:\s*仓库)?|技能(?:仓库)?|知识(?:库|仓库)"
    r"|knowledge(?: base)?",
    "artifact_output": r"(?:文档|产物|输出)仓库|更新(?:已有)?用户手册"
    r"|(?:文档|产物)(?:输出|写入|保存)(?:到|至)?"
    r"|output repo(?:sitory)?|documentation repo(?:sitory)?",
    "execution_context": r"执行上下文|工作(?:目录|仓库)|execution context|working repo",
}
_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
_BARE = re.compile(rf"{_NAME}(?:/{_NAME})+")
_URL = re.compile(
    r"(?:https?://|ssh://git@|git@)"
    r"(?:github\.com|gitlab\.com|bitbucket\.org|code\.byted\.org|git\.byted\.org)"
    r"[/:][A-Za-z0-9_./~-]+"
)
_LABELED = re.compile(
    rf"(?:仓库|repository|repo)[\"']?(?:\s*[：:=]\s*|\s+)[`\"']?"
    rf"({_NAME}(?:/{_NAME})+)",
    re.IGNORECASE,
)
_FAILURE = re.compile(
    r"fatal:|permission denied|access denied|\b(?:403|404)\b|repository not found",
    re.IGNORECASE,
)


def repository_identity(value: object) -> tuple[str, str | None] | None:
    """Normalize explicit identities, preserving unknown host and name case."""
    if not isinstance(value, str):
        return None
    value = value.strip().strip("`\"'")
    host = None
    if value.startswith("git@"):
        match = re.fullmatch(r"git@([^/:]+):(.+)", value)
        if not match:
            return None
        host, value = match.groups()
    elif "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            return None
        host, value = parsed.hostname, parsed.path.lstrip("/")
        # A source page, MR or branch URL identifies the containing repository.
        if host in {"github.com", "bitbucket.org", "code.byted.org", "git.byted.org"}:
            value = "/".join(value.split("/")[:2])
        elif "/-/" in value:
            value = value.split("/-/", 1)[0]
    value = value.rstrip("/").removesuffix(".git")
    if not _BARE.fullmatch(value) or any(
        part in {".", ".."} for part in value.split("/")
    ):
        return None
    return value, host.lower() if host else None


def _walk(value, pointer=""):
    """Walk JSON containers only; pointers always address the original document."""
    yield pointer, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, f"{pointer}/{pointer_part(key)}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{pointer}/{index}")


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _purposes(text: str, offset: int) -> list[str]:
    # Use only the clause before this identity, so another repo's purpose cannot
    # leak across a sentence, list row or semicolon. Ambiguous prose stays unknown.
    prefix = re.split(r"[\n。；;!?！？]", text[:offset])[-1][-8000:]
    # Rich-text links can repeat long URLs; they are not purpose words.
    prefix = _URL.sub("", prefix)[-200:]
    if re.search(r"示例|例如|不要|不需要|dependency|example", prefix, re.IGNORECASE):
        return ["unknown"]
    matches = [
        (match.start(), purpose)
        for purpose, pattern in _PURPOSE_CUES.items()
        for match in re.finditer(pattern, prefix, re.IGNORECASE)
    ]
    if not matches:
        return ["unknown"]
    # The nearest purpose phrase applies; repeated evidence can add more roles.
    return [max(matches)[1]]


def _message_nodes(value, pointer):
    """Do not reinterpret returned files, assistant prose or tool args as users."""
    if isinstance(value, dict):
        if value.get("kind") in {"tool_call", "assistant_message", "agent_narration"}:
            return
        if value.get("span_type") in {"tool-call", "tool_call"}:
            return
        if value.get("role") in {"assistant", "tool"}:
            return
        yield pointer, value
        for key, child in value.items():
            if key not in {
                "result",
                "output",
                "arguments",
                "parsed_arguments",
                "native",
            }:
                yield from _message_nodes(child, f"{pointer}/{pointer_part(key)}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _message_nodes(child, f"{pointer}/{index}")


class _Collector:
    def __init__(self):
        self.records: dict[tuple, dict] = {}
        self.unresolved: list[dict] = []

    def add(
        self, value, category, state, ref, *, branch=None, revision=None, purposes=None
    ):
        identity = repository_identity(value)
        if identity is None:
            return
        name, platform = identity
        record = self.records.setdefault(
            identity,
            {"name": name, "platform": platform, "categories": [], "evidence": []},
        )
        if category not in record["categories"]:
            record["categories"].append(category)
        evidence = {
            **ref,
            "category": category,
            "state": state,
            "rule": f"repository:{category}.v1",
            "purposes": purposes or ["unknown"],
            "platform": platform,
        }
        # These values belong to this evidence, not to every repo in the task.
        for key, field in (("branch", branch), ("revision", revision)):
            if isinstance(field, str) and field:
                evidence[key] = field
        if evidence not in record["evidence"]:
            record["evidence"].append(evidence)

    def text(self, value, category, state, ref):
        for pointer, leaf in _walk(value):
            if isinstance(leaf, dict):
                for key in ("repo", "repository", "repo_name", "repo_url"):
                    if repository_identity(leaf.get(key)):
                        purpose = leaf.get("purpose")
                        self.add(
                            leaf[key],
                            category,
                            state,
                            {
                                **ref,
                                "pointer": ref["pointer"] + pointer + "/" + key,
                                "excerpt": leaf[key],
                            },
                            branch=leaf.get("branch"),
                            revision=leaf.get("revision"),
                            purposes=[purpose]
                            if isinstance(purpose, str) and purpose in PURPOSE_LABELS
                            else None,
                        )
            if not isinstance(leaf, str):
                continue
            # Bare org/repo tokens are accepted only with an explicit repo label.
            matches = [(m[0], m.start()) for m in _URL.finditer(leaf)]
            matches += [(m[1], m.start(1)) for m in _LABELED.finditer(leaf)]
            for identity, offset in matches:
                excerpt = leaf[max(0, offset - 80) : offset + len(identity) + 80]
                evidence = {
                    **ref,
                    "pointer": ref["pointer"] + pointer,
                    "excerpt": excerpt,
                }
                # Text alone proves a mention. Examples/dependency lists never
                # become actual-use evidence, even when the URL is well formed.
                self.add(
                    identity,
                    category,
                    state,
                    evidence,
                    purposes=_purposes(leaf, offset),
                )


def _source(doc, pointer, value):
    return {
        "dataset": doc["dataset"],
        "path": doc["reference"]["path"],
        "pointer": pointer,
        "excerpt": _text(value)[:600],
    }


def _task_records(collector, doc, task_id):
    content = doc["content"]
    if not isinstance(content, dict):
        return
    for index, item in enumerate(content.get("items", [])):
        if not isinstance(item, dict):
            continue
        task = item.get("task")
        if (
            not isinstance(task, dict)
            or str(task.get("task_id", item.get("task_id"))) != task_id
        ):
            continue
        base = f"{doc['reference']['pointer']}/items/{index}/task"
        value = task.get("repo_path")
        ref = _source(doc, base + "/repo_path", value)
        if value and repository_identity(value) is None:
            collector.unresolved.append({"reason": "local_or_unknown_repo_path", **ref})
        collector.add(value, "platform_field", "registered", ref)
        commits = decoded(task.get("commit_list"))
        if not isinstance(commits, list):
            continue
        for ci, commit in enumerate(commits):
            if not isinstance(commit, dict):
                continue
            pointer = base + "/commit_list"
            if isinstance(task.get("commit_list"), list):
                pointer += f"/{ci}"
            collector.add(
                commit.get("repo_name"),
                "commit_metadata",
                "commit_recorded",
                _source(doc, pointer, commit),
                branch=commit.get("branch"),
                revision=commit.get("commit_id", commit.get("commit_hash")),
            )


def _messages(collector, doc):
    for pointer, node in _message_nodes(doc["content"], doc["reference"]["pointer"]):
        # Legacy traces put the engine prompt in a JSON-encoded input string.
        # Read only the named runtime section, never the surrounding skill rules.
        if node.get("span_type") in {"agent-run", "model-call"}:
            raw = node.get("input")
            prompt = decoded(raw)
            if isinstance(prompt, str):
                for match in re.finditer(
                    r"^## Worker Runtime Context\s*\n.*?(?=^## |\Z)",
                    prompt,
                    re.MULTILINE | re.DOTALL,
                ):
                    ref = _source(doc, pointer + "/input", match[0])
                    if prompt != raw:
                        ref["encoding"] = "json-string"
                    _runtime_text(collector, match[0], ref)
        if node.get("kind") == "user_message":
            payload = node.get("payload", {})
            if not isinstance(payload, dict):
                continue
            base = pointer + "/payload"
            if isinstance(payload.get("data"), dict):
                payload, base = payload["data"], base + "/data"
            for key in ("text", "content"):
                if key in payload:
                    collector.text(
                        payload[key],
                        "user_specified",
                        "mentioned",
                        _source(doc, base + "/" + key, payload[key]),
                    )
        # Raw message arrays are supported, but only inside explicit messages
        # containers, never arbitrary role fields in a tool result.
        if re.search(r"/messages/\d+$", pointer) and node.get("role") == "user":
            value = node.get("content")
            collector.text(
                value,
                "user_specified",
                "mentioned",
                _source(doc, pointer + "/content", value),
            )
        sections = node.get("contextSections")
        if not isinstance(sections, list):
            continue
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            title = section.get("title", section.get("label", section.get("name", "")))
            if title != "Worker Runtime Context":
                continue
            # Runtime section mentions are not automatically mounted: explicit
            # structured repository entries are the only mounted-state rule.
            base = f"{pointer}/contextSections/{index}"
            for key in ("text", "content"):
                if isinstance(section.get(key), str):
                    _runtime_text(
                        collector,
                        section[key],
                        _source(doc, base + "/" + key, section[key]),
                    )
            for rp, repo in _walk(section, base):
                if not isinstance(repo, dict):
                    continue
                value = repo.get("repo", repo.get("repo_name", repo.get("repo_url")))
                if value is not None:
                    collector.add(
                        value,
                        "runtime_context",
                        "mounted",
                        _source(doc, rp, repo),
                        branch=repo.get("branch"),
                        revision=repo.get("revision"),
                        purposes=["execution_context"],
                    )
            collector.text(
                section, "runtime_context", "mentioned", _source(doc, base, section)
            )


def _runtime_text(collector, text, ref):
    # This explicit platform list names mounted repositories, unlike a random
    # local path in prose. Keep its per-row branch, and leave the host unknown.
    if "工作目录下当前的仓库列表为" not in text:
        return
    for match in re.finditer(
        rf"^\s*-\s+\./({_NAME}(?:/{_NAME})+)/\s*\(branch:\s*([^\n)]+)\)",
        text,
        re.MULTILINE,
    ):
        collector.add(
            match[1],
            "runtime_context",
            "mounted",
            {**ref, "excerpt": match[0].strip()},
            branch=match[2].strip(),
            purposes=["execution_context"],
        )


def _tools(collector: _Collector, events: list[Event]):
    for event in events:
        if event.kind != "tool_call" or event.name.split(":", 1)[0] not in {
            "bash",
            "Bash",
            "sh",
            "shell",
            "exec_command",
        }:
            continue
        arguments = object_value(event.arguments)
        command = arguments.get("command", arguments.get("cmd"))
        if not isinstance(command, str):
            continue
        segments = command_segments(command)
        output = _text(event.output)
        result = object_value(event.output)
        exit_code = event.exit_code
        if exit_code is None and type(result.get("exit_code")) is int:
            exit_code = result["exit_code"]
        success = (
            exit_code == 0
            and not event.output_truncated
            and event.status not in {"cancelled", "canceled"}
        )
        failed = (
            exit_code not in {None, 0}
            or event.status
            in {
                "error",
                "failed",
                "failure",
            }
            or bool(_FAILURE.search(output))
        )
        for tokens in segments:
            if not tokens or tokens[0] != "git":
                continue
            tokens = tokens[1:]
            if tokens[:1] == ["-C"] and len(tokens) > 2:
                tokens = tokens[2:]
            ref = {**asdict(event.evidence), "excerpt": command[:600]}
            if event.output is not None:
                ref["result_excerpt"] = output[:600]
            if tokens[:1] in (["clone"], ["ls-remote"]):
                branch = None
                if tokens[0] == "clone":
                    for index, option in enumerate(tokens):
                        if option in {"-b", "--branch"} and index + 1 < len(tokens):
                            branch = tokens[index + 1]
                        elif option.startswith("--branch="):
                            branch = option.removeprefix("--branch=")
                for token in tokens[1:]:
                    if token.startswith(("https://", "http://", "ssh://", "git@")):
                        # Compound shell status cannot identify which clone failed.
                        state = "prepare_attempted"
                        if len(segments) == 1:
                            state = (
                                "prepare_failed"
                                if failed
                                else ("prepared" if success else "prepare_attempted")
                            )
                        if tokens[0] == "ls-remote":
                            state = {
                                "prepare_failed": "access_failed",
                                "prepared": "remote_verified",
                                "prepare_attempted": "access_attempted",
                            }[state]
                        offset = command.find(token)
                        collector.add(
                            token,
                            "tool_execution",
                            state,
                            {
                                **ref,
                                "excerpt": command[
                                    max(0, offset - 80) : offset + len(token) + 80
                                ],
                            },
                            branch=branch,
                        )
            elif (
                len(segments) == 1
                and success
                and not failed
                and (
                    tokens == ["remote", "-v"]
                    or tokens == ["remote", "get-url", "origin"]
                    or tokens == ["config", "--get", "remote.origin.url"]
                )
            ):
                for match in _URL.finditer(output):
                    # Include the actual return as well as the command.
                    collector.add(
                        match[0],
                        "tool_execution",
                        "remote_verified",
                        {**ref, "excerpt": match[0], "command": command[:600]},
                    )


def extract_repositories(
    documents: list[dict], events: list[Event], task_id: str
) -> dict:
    collector = _Collector()
    repository_events = list(events)
    for doc in documents:
        if doc["dataset"] == "task":
            _task_records(collector, doc, task_id)
        elif doc["dataset"] in {"execution", "conversation"}:
            _messages(collector, doc)
            if doc["dataset"] == "conversation":
                ref = doc["reference"]
                try:
                    repository_events.extend(
                        conversation_events(
                            doc["content"],
                            ref.get("content_kind"),
                            Evidence("conversation", ref["path"], ref["pointer"]),
                            include_projected=True,
                        )
                    )
                except (ValueError, TypeError):
                    # The analyzer reports unsupported event shapes separately.
                    pass
    _tools(collector, deduplicate(repository_events))
    # Combine a bare name with a single observed host, retaining the original
    # host (including unknown) on each evidence. Conflicting hosts stay separate.
    for (name, platform), unknown in list(collector.records.items()):
        if platform is not None:
            continue
        known = [
            record
            for (other_name, host), record in collector.records.items()
            if other_name == name and host is not None
        ]
        if len(known) == 1:
            record = known[0]
            record["categories"] = sorted(
                set(record["categories"] + unknown["categories"])
            )
            record["evidence"].extend(unknown["evidence"])
            record["identity_resolution"] = "single_observed_host_for_name"
            del collector.records[(name, None)]
    records = sorted(
        collector.records.values(),
        key=lambda repo: (repo["name"], repo["platform"] or ""),
    )
    for record in records:
        record["categories"].sort()
        record["states"] = sorted({ref["state"] for ref in record["evidence"]})
        purposes = {p for ref in record["evidence"] for p in ref["purposes"]}
        record["purposes"] = sorted(purposes - {"unknown"}) or ["unknown"]
        record["association"] = (
            "candidate" if record["states"] == ["mentioned"] else "observed"
        )
    return {
        "version": "repository-extraction.v1",
        "method": "rules",
        "coverage": "supported_evidence_only",
        "status": "observed" if records else "unknown",
        "repositories": records,
        "category_labels": CATEGORY_LABELS,
        "purpose_labels": PURPOSE_LABELS,
        "unresolved": collector.unresolved,
        "limitations": [
            "Only explicit repository identities in supported evidence are extracted.",
            "Mentions are candidates, not proof of use; unclear purposes stay unknown.",
            "Local paths and project IDs do not establish remote repository identity.",
            "External attachments are not fetched; unseen content remains unknown.",
        ],
    }
