"""Read the contract locally and produce explainable test admission records."""

import json
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from flux_task_service.export_contract import ContractError, TaskDataReader

from .adapters import (
    conversation_events,
    decoded,
    deduplicate,
    event_key,
    execution_events,
    object_value,
    stable_id,
)
from .commands import classify, command_segments, is_review_command
from .models import Evidence, TaggingPolicy, TestRun
from .presentation import compact_result
from .reports import (
    code_guard_report,
    counts,
    gui_runs,
    junit_report,
    output_counts,
    result_state,
    unit_report,
)
from .repositories import extract_repositories

ANALYZER_VERSION = "1.5"
FAILURE_STATUSES = {"error", "failed", "failure"}
SHELL_NAMES = {"bash", "sh", "shell", "exec_command", "Bash"}


def _load(reader: TaskDataReader) -> tuple[dict, list[dict], list[dict]]:
    datasets, documents, errors = {}, [], []
    for name, entry in reader.manifest.datasets.items():
        datasets[name] = entry.model_dump()
        if entry.status not in {"available", "partial"} or name == "raw_spans":
            continue
        for ref in entry.files:
            if ref.format == "bytes.v1":
                continue
            try:
                content = reader.read_file(ref)
            except ContractError as exc:
                errors.append(
                    {
                        "dataset": name,
                        "path": ref.path,
                        "code": exc.code,
                        "message": str(exc),
                    }
                )
            else:
                documents.append(
                    {"dataset": name, "reference": ref.model_dump(), "content": content}
                )
    return datasets, documents, errors


def _ref(doc: dict) -> Evidence:
    ref = doc["reference"]
    return Evidence(doc["dataset"], ref["path"], ref["pointer"])


def _task_info(documents: list[dict], task_id: str) -> dict:
    selected = {}
    for doc in documents:
        if doc["dataset"] != "task":
            continue
        for item in doc["content"].get("items", []):
            if not isinstance(item, dict):
                continue
            task = object_value(item.get("task"))
            identity = task.get("task_id", item.get("task_id"))
            if str(identity) != task_id:
                continue
            for key in (
                "title",
                "task_status",
                "task_is_completed",
                "work_mode",
                "worker_mode",
                "task_source",
                "agents",
                "task_classification",
                "related_logic_tasks",
                "dev_info",
                "dev_metrics",
                "commit_list",
                "repo_path",
                "related_sessions",
            ):
                if key in task:
                    selected[key] = decoded(task[key])
    return selected


def _tool_activity(
    events, policy: TaggingPolicy
) -> tuple[list[TestRun], list[dict], dict]:
    runs, activities = [], []
    stats = Counter()
    for event in events:
        if event.kind != "tool_call":
            continue
        stats["tool_call_count"] += 1
        stats["tool_failed_count"] += event.status in FAILURE_STATUSES
        stats["tool_cancelled_count"] += event.status in {"cancelled", "canceled"}
        stats["nonzero_exit_count"] += (
            event.exit_code is not None and event.exit_code != 0
        )
        stats["unknown_exit_count"] += event.exit_code is None
        evidence = event.evidence
        arguments = object_value(event.arguments)
        name = event.name.split(":", 1)[0]
        if name in {"edit", "apply_patch", "write", "Write", "Edit"}:
            paths = set()
            changes = arguments.get("changes")
            if isinstance(changes, dict):
                paths.update(changes)
            value = decoded(event.output)
            updates = value if isinstance(value, list) else [value]
            for update in updates:
                update = object_value(update)
                if update.get("type") == "diff" and isinstance(update.get("path"), str):
                    paths.add(update["path"])
            activities.append(
                {
                    "kind": "file_edit",
                    "paths": sorted(paths),
                    "evidence": asdict(evidence),
                }
            )
        if name in {"ask_user_questions", "request_user_input"}:
            activities.append({"kind": "user_question", "evidence": asdict(evidence)})
        skill = arguments.get("skill")
        if skill in policy.test_skills:
            activities.append(
                {
                    "kind": "test_skill_invocation",
                    "skill": skill,
                    "evidence": asdict(replace(evidence, rule="skill-argument")),
                }
            )
        if name not in SHELL_NAMES:
            continue
        stats["shell_call_count"] += 1
        command = arguments.get("command", arguments.get("cmd"))
        if not isinstance(command, str) or not command.strip():
            stats["missing_shell_arguments"] += 1
            continue
        segments = command_segments(command)
        if not segments:
            stats["unparsed_shell_commands"] += 1
        tests = []
        for tokens in segments:
            if tokens[:1] in (["cat"], ["read"]):
                for skill_name in policy.test_skills:
                    if any(
                        token.endswith(f"/{skill_name}/SKILL.md")
                        for token in tokens[1:]
                    ):
                        activities.append(
                            {
                                "kind": "test_skill_document_read",
                                "skill": skill_name,
                                "evidence": asdict(
                                    replace(evidence, rule="skill-file-read")
                                ),
                            }
                        )
            match = classify(tokens, policy)
            if not match:
                continue
            kind, detail = match
            ref = replace(
                evidence, rule=f"command:{kind}:{detail}", excerpt=command[:500]
            )
            activities.append(
                {
                    "kind": kind,
                    "detail": detail,
                    "evidence": asdict(ref),
                    "call_status": event.status,
                }
            )
            if kind == "test":
                tests.append((detail, ref))
        # A whole shell return must not be assigned to each subcommand.
        # Multiple test commands in one call are kept as one invocation per type;
        # text results are parsed only when the call contains one command segment.
        for test_type in sorted({detail for detail, _ in tests}):
            ref = next(ref for detail, ref in tests if detail == test_type)
            values = (
                output_counts(event.output, test_type) if len(segments) == 1 else None
            )
            if event.output_truncated:
                values = None
            state = result_state(values) if values else "attempted"
            runs.append(
                TestRun(
                    run_id=event_key(event) + ":" + test_type,
                    test_type=test_type,
                    origin="tool_invocation",
                    association="matched",
                    execution_state=state,
                    counts=values or counts(),
                    reported_issue_count=None,
                    evidence=[ref],
                    command=command,
                    invocation_status=event.status,
                )
            )
    return runs, activities, dict(stats)


def _report_runs(reader, documents, sessions, policy, errors, events):
    runs = []
    fingerprint = []
    original_paths = {}
    generated_reports = {}
    for doc in documents:
        if (
            doc["dataset"] == "artifacts"
            and doc["reference"]["role"] == "download-index"
        ):
            for download in doc["content"]:
                if download.get("status") == "ok" and isinstance(
                    download.get("file"), str
                ):
                    original_paths[
                        download["file"].removeprefix(
                            f"tasks/{reader.manifest.task_id}/"
                        )
                    ] = download.get("path")
    for event in events:
        if event.kind != "tool_call" or event.status not in {"success", "succeeded"}:
            continue
        command = object_value(event.arguments).get("command")
        if not isinstance(command, str):
            continue
        for tokens in command_segments(command):
            if (
                len(tokens) > 2
                and tokens[1].endswith("/generate_report.py")
                and "/bits-code-guard" in tokens[1]
                and "--report-dir" in tokens
            ):
                position = tokens.index("--report-dir") + 1
                if position < len(tokens):
                    generated_reports[
                        tokens[position].rstrip("/") + "/report.jsonl"
                    ] = replace(
                        event.evidence,
                        rule="code-guard-report-generation",
                        excerpt=command[:500],
                    )
    for doc in documents:
        if doc["dataset"] == "gui":
            found = gui_runs(
                doc["content"], _ref(doc), reader.manifest.task_id, sessions
            )
            runs.extend(found)
            if not found:
                errors.append(
                    {
                        "dataset": "gui",
                        "path": doc["reference"]["path"],
                        "code": "UNSUPPORTED_TEST_RESULT_SHAPE",
                    }
                )
    entry = reader.manifest.datasets["artifacts"]
    if entry.status not in {"available", "partial"}:
        return runs, fingerprint
    for ref in entry.files:
        if ref.role != "artifact" or Path(ref.path).suffix.lower() not in {
            ".json",
            ".xml",
            ".jsonl",
        }:
            continue
        path = reader.root / ref.path
        try:
            if path.stat().st_size > policy.max_artifact_bytes:
                errors.append(
                    {
                        "dataset": "artifacts",
                        "path": ref.path,
                        "code": "ARTIFACT_ANALYSIS_LIMIT",
                    }
                )
                continue
            raw = reader.read_file(ref)
            text = raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw)
        except (ContractError, OSError, UnicodeError) as exc:
            errors.append(
                {
                    "dataset": "artifacts",
                    "path": ref.path,
                    "code": exc.code
                    if isinstance(exc, ContractError)
                    else "ARTIFACT_UNREADABLE",
                }
            )
            continue
        fingerprint.append({"path": ref.path, "hash": stable_id(text)})
        evidence = Evidence("artifacts", ref.path, "")
        if Path(ref.path).suffix.lower() == ".jsonl":
            binding = generated_reports.get(original_paths.get(ref.path))
            run = code_guard_report(text, evidence, binding is not None)
            if run and binding:
                run.evidence.append(binding)
        elif Path(ref.path).suffix.lower() == ".xml":
            run = junit_report(text, evidence)
        else:
            data = object_value(text)
            run = unit_report(data, evidence, reader.manifest.task_id, sessions)
        if run:
            runs.append(run)
    return runs, fingerprint


def _skill_records(reader, errors: list[dict]) -> list[dict]:
    records = []
    entry = reader.manifest.datasets["artifacts"]
    if entry.status not in {"available", "partial"}:
        return records
    for ref in entry.files:
        if ref.role != "artifact" or not ref.path.endswith("/skill_event.jsonl"):
            continue
        try:
            if (reader.root / ref.path).stat().st_size > 1_000_000:
                continue
            raw = reader.read_file(ref)
            text = raw.decode("utf-8") if isinstance(raw, bytes) else ""
            for index, line in enumerate(text.splitlines()):
                event = object_value(line)
                if event.get("action") in {"begin", "end"} and isinstance(
                    event.get("skill_name"), str
                ):
                    records.append(
                        {
                            "skill": event["skill_name"],
                            "action": event["action"],
                            "skill_unique_id": event.get("skill_unique_id"),
                            "timestamp": event.get("timestamp"),
                            "path": ref.path,
                            "line": index + 1,
                        }
                    )
        except (ContractError, OSError, UnicodeError) as exc:
            errors.append(
                {
                    "dataset": "artifacts",
                    "path": ref.path,
                    "code": exc.code
                    if isinstance(exc, ContractError)
                    else "ARTIFACT_UNREADABLE",
                }
            )
    return records


def _merge_runs(runs: list[TestRun], errors: list[dict]) -> list[TestRun]:
    unique = {}
    for run in runs:
        key = (run.origin, run.run_id)
        previous = unique.get(key)
        if previous is None:
            unique[key] = run
        elif (previous.counts, previous.association) == (run.counts, run.association):
            previous.evidence.extend(run.evidence)
        else:
            errors.append({"code": "CONFLICTING_RUN_RESULTS", "run_id": run.run_id})
            # Do not pick a winning snapshot without an explicit ordering contract.
            previous.association = "conflict"
            unique[key + (stable_id(run.to_dict()),)] = run
            run.association = "conflict"
    return list(unique.values())


def _coverage(
    datasets: dict, documents: list[dict], stats: dict, errors: list[dict]
) -> dict:
    complete_sources = []
    for doc in documents:
        name, payload = doc["dataset"], doc["content"]
        if (
            name not in {"execution", "conversation"}
            or datasets[name]["complete"] is not True
        ):
            continue
        if any(error.get("dataset") == name for error in errors):
            continue
        scope = datasets[name]["scope"]
        if scope.get("window") or scope.get("omitted"):
            continue
        if name == "execution":
            meta = (
                object_value(payload.get("meta")) if isinstance(payload, dict) else {}
            )
            if object_value(meta.get("projection")).get("mode") == "metadata_only":
                continue
            if doc["reference"]["format"] in {
                "trace-read-model.v2",
                "flux.normalized-spans.v1",
            }:
                complete_sources.append(name)
        elif doc["reference"].get("content_kind") == "full-trace":
            complete_sources.append(name)
    return {
        "rule_search_complete": bool(complete_sources)
        and stats.get("missing_shell_arguments", 0) == 0
        and stats.get("unparsed_shell_commands", 0) == 0,
        "complete_sources": sorted(set(complete_sources)),
        "scope": {
            name: datasets[name]["scope"]
            for name in ("execution", "conversation", "gui", "artifacts")
        },
        "meaning": "Coverage of supported engineering rules, not proof of no tests.",
    }


def _reported_scenarios(task: dict) -> list[dict]:
    signals = []
    categories = {
        "测试",
        "完整测试",
        "接口测试",
        "单元测试",
        "GUI 测试",
        "集成测试",
        "性能测试",
        "code review",
    }
    segments = task.get("related_logic_tasks")
    if isinstance(segments, list):
        for index, segment in enumerate(segments):
            if isinstance(segment, dict) and any(
                segment.get(key) in categories
                for key in ("level_1_category", "level_2_category")
            ):
                signals.append(
                    {
                        "kind": "platform_test_classification",
                        "source": "task",
                        "segment_index": index,
                        "value": segment,
                    }
                )
    classification = task.get("task_classification")
    if (
        isinstance(classification, dict)
        and classification.get("category") in categories
    ):
        signals.append(
            {
                "kind": "platform_test_classification",
                "source": "task",
                "value": classification,
            }
        )
    return signals


EVIDENCE_CATEGORIES = {
    "test_execution_call": "测试执行调用",
    "test_skill_invocation": "测试技能调用",
    "defect_review_execution": "缺陷审查执行",
    "test_report": "测试或审查报告",
    "platform_test_scenario": "平台明确报告测试场景",
}
SUPPORTING_CATEGORIES = {"test_execution_call", "platform_test_scenario"}


def _evidence_matches(runs, activities, scenario_signals, policy):
    sources = {category: [] for category in EVIDENCE_CATEGORIES}
    for run in runs:
        if (
            run.association != "matched"
            or run.test_type not in policy.allowed_test_types
        ):
            continue
        refs = [asdict(ref) for ref in run.evidence]
        if run.origin == "tool_invocation":
            review = any(
                is_review_command(tokens)
                for tokens in command_segments(run.command or "")
            )
            sources[
                "defect_review_execution" if review else "test_execution_call"
            ].extend(refs)
            if run.execution_state in {"executed", "not_executed"}:
                sources["test_report"].extend(refs)
        else:
            sources["test_report"].extend(refs)
    skill_types = {"bits-unit-test-gen": "unit", "flux-api-test": "api"}
    for activity in activities:
        if (
            activity["kind"] == "test_skill_invocation"
            and skill_types.get(activity.get("skill"), "other")
            in policy.allowed_test_types
        ):
            sources["test_skill_invocation"].append(activity)
    if "other" in policy.allowed_test_types:
        sources["platform_test_scenario"].extend(scenario_signals)
    return [
        {
            "category": category,
            "label": label,
            "matched": bool(sources[category]),
            "strength": "supporting" if category in SUPPORTING_CATEGORIES else "strong",
            "sources": sources[category],
        }
        for category, label in EVIDENCE_CATEGORIES.items()
    ]


def _decision(runs, activities, coverage, policy, errors, scenario_signals, matches):
    eligible = [
        run
        for run in runs
        if run.association == "matched" and run.test_type in policy.allowed_test_types
    ]
    problem = any(
        (run.counts.get("failed") or 0) > 0 or (run.reported_issue_count or 0) > 0
        for run in eligible
    )
    categories = {match["category"] for match in matches if match["matched"]}
    skill_invoked = any(
        activity["kind"] == "test_skill_invocation" for activity in activities
    )
    if len(categories) >= 3 or categories - SUPPORTING_CATEGORIES:
        level = "priority_candidate" if len(categories) >= 3 else "ordinary_candidate"
        reason = (
            "THREE_OR_MORE_EVIDENCE_CATEGORIES"
            if len(categories) >= 3
            else "STRONG_TEST_EVIDENCE_OBSERVED"
        )
        if policy.priority_only and level != "priority_candidate":
            return level, "exclude", ["PRIORITY_CANDIDATE_REQUIRED"]
        if policy.require_problem_signal and not problem:
            return level, "exclude", ["PROBLEM_SIGNAL_REQUIRED_NOT_OBSERVED"]
        return level, "keep", [reason]
    if categories:
        return "review", "hold", ["SUPPORTING_TEST_EVIDENCE_ONLY"]
    if any(run.association != "matched" for run in runs):
        return "review", "hold", ["TEST_EVIDENCE_ASSOCIATION_UNCONFIRMED"]
    if runs and not eligible:
        return "excluded", "exclude", ["TEST_TYPE_NOT_ALLOWED"]
    if scenario_signals or skill_invoked:
        return "review", "hold", ["TEST_TYPE_UNKNOWN"]
    if (
        any(
            error.get("dataset") in {"execution", "conversation", "gui", "artifacts"}
            or error.get("code") == "CONFLICTING_RUN_RESULTS"
            for error in errors
        )
        or not coverage["rule_search_complete"]
    ):
        return "review", "hold", ["INSUFFICIENT_ANALYSIS_COVERAGE"]
    if any(activity["kind"].startswith("test_skill") for activity in activities):
        return "excluded", "exclude", ["TEST_SETUP_WITHOUT_EXECUTION_EVIDENCE"]
    return "excluded", "exclude", ["NO_RECOGNIZED_TEST_EXECUTION_EVIDENCE"]


def analyze_task(
    task_dir: str | Path,
    *,
    policy: TaggingPolicy | None = None,
    allow_legacy: bool = False,
) -> dict:
    """Analyze local, finalized exports. No network, source commands or AI execute."""
    policy = policy or TaggingPolicy()
    reader = TaskDataReader(task_dir, allow_legacy=allow_legacy)
    if not reader.manifest.finalized:
        raise ContractError("MANIFEST_NOT_FINALIZED", "Task export is still unfinished")
    datasets, documents, errors = _load(reader)
    task = _task_info(documents, reader.manifest.task_id)
    events = []
    for doc in documents:
        ref, payload = doc["reference"], doc["content"]
        try:
            if doc["dataset"] == "execution" and ref["role"] == "data":
                events.extend(execution_events(payload, ref["format"], _ref(doc)))
            elif doc["dataset"] == "conversation" and ref["role"] == "data":
                if ref.get("content_kind") is None:
                    errors.append(
                        {
                            "dataset": "conversation",
                            "path": ref["path"],
                            "code": "MISSING_CONTENT_KIND",
                        }
                    )
                events.extend(
                    conversation_events(payload, ref.get("content_kind"), _ref(doc))
                )
        except (ValueError, TypeError) as exc:
            errors.append(
                {
                    "dataset": doc["dataset"],
                    "path": ref["path"],
                    "code": "UNSUPPORTED_EVENT_SHAPE",
                    "message": str(exc),
                }
            )
    events = deduplicate(events)
    sessions = {
        event.evidence.session_id for event in events if event.evidence.session_id
    }
    for session in task.get("related_sessions") or []:
        if isinstance(session, dict) and session.get("session_id"):
            sessions.add(str(session["session_id"]))
    runs, activities, stats = _tool_activity(events, policy)
    reports, _fingerprint = _report_runs(
        reader, documents, sessions, policy, errors, events
    )
    skills = _skill_records(reader, errors)
    stats["observed_model_call_count"] = sum(
        event.kind == "model_call" for event in events
    )
    stats["observed_agent_run_count"] = sum(
        event.kind == "agent_run" for event in events
    )
    for skill in skills:
        if skill["skill"] in policy.test_skills:
            activities.append({"kind": "test_skill_telemetry", **skill})
    runs = _merge_runs(runs + reports, errors)
    coverage = _coverage(datasets, documents, stats, errors)
    scenario_signals = _reported_scenarios(task)
    evidence_matches = _evidence_matches(runs, activities, scenario_signals, policy)
    level, decision, reasons = _decision(
        runs, activities, coverage, policy, errors, scenario_signals, evidence_matches
    )
    matched = [run for run in runs if run.association == "matched"]
    executed = [run for run in matched if run.execution_state == "executed"]
    result_records = [
        run
        for run in matched
        if run.origin != "tool_invocation"
        or run.execution_state in {"executed", "not_executed"}
    ]
    evidence_level = (
        "result_observed"
        if result_records
        else (
            "invocation_observed"
            if any(run.origin == "tool_invocation" for run in matched)
            or any(
                activity["kind"] == "test_skill_invocation" for activity in activities
            )
            else "setup_only"
            if any(a["kind"].startswith("test_skill") for a in activities)
            else "scenario_reported"
            if scenario_signals
            else "unknown"
            if runs
            else "no_match"
            if coverage["rule_search_complete"]
            else "unknown"
        )
    )
    metrics = [doc["content"] for doc in documents if doc["dataset"] == "metrics"]
    result = {
        "task_id": reader.manifest.task_id,
        "analyzer_version": ANALYZER_VERSION,
        "analyzed_at": datetime.now(UTC).isoformat(),
        "core": {
            "test_evidence_level": evidence_level,
            "evidence_matches": evidence_matches,
            "evidence_category_count": sum(
                match["matched"] for match in evidence_matches
            ),
            "test_types": sorted({run.test_type for run in matched}),
            "test_scenario_signals": scenario_signals,
            "test_activity_observed": bool(matched)
            or any(
                activity["kind"] == "test_skill_invocation" for activity in activities
            ),
            "observed_test_run_count": len(executed),
            "test_result_record_count": len(result_records),
            "run_count_basis": "recognized_result_records_after_id_deduplication",
            "cross_source_unique_run_count": len(executed)
            if len({run.origin for run in executed}) <= 1
            else None,
            "problem_signal": any(
                (run.counts.get("failed") or 0) > 0
                or (run.reported_issue_count or 0) > 0
                for run in matched
            )
            if matched
            else None,
            "runs": [run.to_dict() for run in runs],
            "coverage": coverage,
        },
        "admission": {
            "candidate_level": level,
            "decision": decision,
            "reason_codes": reasons,
            "matched_evidence_categories": [
                match["category"] for match in evidence_matches if match["matched"]
            ],
            "evidence_category_count": sum(
                match["matched"] for match in evidence_matches
            ),
        },
        "recorded_only": {
            "platform": task,
            "repository_extraction": extract_repositories(
                documents, events, reader.manifest.task_id
            ),
            "export_status": reader.manifest.task_status,
            "legacy": reader.manifest.legacy,
            "tool_statistics": stats,
            "activities": activities,
            "skill_events": skills,
            "artifact_files": [
                ref.model_dump()
                for ref in reader.manifest.datasets["artifacts"].files
                if ref.role == "artifact"
            ],
            "metrics": metrics,
            "versions": [
                doc["content"] for doc in documents if doc["dataset"] == "versions"
            ],
        },
        "datasets": datasets,
        "analysis_errors": errors,
        "export_errors": [error.model_dump() for error in reader.manifest.errors],
    }
    return compact_result(result)
