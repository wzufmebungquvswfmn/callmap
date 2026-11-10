"""Bounded human-readable summaries; originals remain in the input export."""

from collections import Counter

LEVELS = {
    "priority_candidate": "优先候选",
    "ordinary_candidate": "普通候选",
    "review": "待复核",
    "excluded": "排除",
}
DECISIONS = {"keep": "保留", "hold": "待复核", "exclude": "排除"}
REASONS = {
    "THREE_OR_MORE_EVIDENCE_CATEGORIES": "命中三类及以上测试证据",
    "STRONG_TEST_EVIDENCE_OBSERVED": "命中技能调用、审查执行或报告证据",
    "SUPPORTING_TEST_EVIDENCE_ONLY": "仅有测试执行调用或平台分类，需复核",
    "TEST_EVIDENCE_ASSOCIATION_UNCONFIRMED": "测试证据归属尚未确认",
    "INSUFFICIENT_ANALYSIS_COVERAGE": "未识别到充分证据，分析覆盖不足",
    "TEST_TYPE_NOT_ALLOWED": "测试类型不在允许范围",
    "TEST_TYPE_UNKNOWN": "无法确认是否属于允许的测试类型",
    "TEST_SETUP_WITHOUT_EXECUTION_EVIDENCE": "仅发现测试准备，未发现执行证据",
    "NO_RECOGNIZED_TEST_EXECUTION_EVIDENCE": "未命中已知测试证据",
    "PRIORITY_CANDIDATE_REQUIRED": "当前策略只保留优先候选",
    "PROBLEM_SIGNAL_REQUIRED_NOT_OBSERVED": "当前策略要求问题线索，尚未观察到",
    "MANIFEST_MISSING": "旧导出缺少数据清单，需启用历史适配或重新导出",
    "MANIFEST_NOT_FINALIZED": "输入导出尚未完成",
    "TAGGING_CANCELLED": "停止前尚未处理",
    "TASK_ANALYSIS_FAILED": "任务内容无法分析",
}
COUNT_KEYS = (
    "mr_count",
    "push_count",
    "test_count",
    "code_review_count",
    "defect_found_count",
    "defect_found_cr_count",
    "defect_found_sdt_count",
    "defect_fixed_count",
)


def brief(value, limit=140):
    if not isinstance(value, str):
        return value
    return " ".join(value.split())[:limit]


def _errors(errors):
    grouped = Counter(
        (error.get("dataset"), error.get("code", "UNKNOWN")) for error in errors
    )
    return [
        {"dataset": dataset, "code": code, "count": count}
        for (dataset, code), count in grouped.items()
    ]


def _unique_categories(segments: list) -> list[dict]:
    """Keep every distinct platform label, without segment timing/message detail."""
    categories = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        category = {
            key: segment[key]
            for key in (
                "level_1_category",
                "level_2_category",
                "level_3_category",
                "story_stage",
            )
            if isinstance(segment.get(key), str) and segment[key]
        }
        if category and category not in categories:
            categories.append(category)
    return categories


def compact_result(result: dict) -> dict:
    """Compress after rule evaluation; never change the admission decision."""
    core = result.get("core", {})
    admission = result["admission"]
    other = result.get("recorded_only", {})
    datasets = result.get("datasets", {})
    task = other.get("platform", {})
    matches = core.get("evidence_matches", [])
    references = []
    locations = {}

    def reference(source):
        ref = source.get("evidence", source)
        if not ref.get("path") and source.get("source") == "task":
            files = datasets.get("task", {}).get("files", [])
            ref = next((value for value in files if value.get("role") == "data"), {})
        if not ref.get("path"):
            return None
        key = (ref["path"], ref.get("pointer", ""))
        if key in locations:
            return locations[key]
        if len(references) >= 8:
            return None
        identity = f"E{len(references) + 1}"
        references.append({"id": identity, "path": key[0], "pointer": key[1]})
        locations[key] = identity
        return identity

    runs = core.get("runs", [])
    evidence = []
    for match in matches:
        sources = match.get("sources", [])
        description = "未命中"
        ref = None
        if sources:
            source = sources[0]
            ref = reference(source)
            payload = source.get("evidence", source)
            description = brief(payload.get("excerpt"))
            if not description:
                if source.get("skill"):
                    description = f"调用 {source['skill']}"
                elif source.get("source") == "task":
                    categories = source.get("value", {})
                    description = brief(
                        " / ".join(
                            str(categories[key])
                            for key in (
                                "level_1_category",
                                "level_2_category",
                                "category",
                            )
                            if categories.get(key)
                        )
                    )
                else:
                    description = f"识别到 {len(sources)} 条记录"
        if match["category"] == "test_report" and match["matched"]:
            issue_counts = [
                run["reported_issue_count"]
                for run in runs
                if run.get("association") == "matched"
                and run.get("reported_issue_count") is not None
            ]
            if issue_counts:
                description = f"已生成报告，单份报告最多记录 {max(issue_counts)} 个问题"
        item = {
            "category": match["category"],
            "label": match["label"],
            "matched": match["matched"],
            "summary": description or "已命中",
        }
        if ref:
            item["reference"] = ref
        evidence.append(item)

    # Show reports, failed calls and other key records; this is a sample, not a log.
    important = sorted(
        runs,
        key=lambda run: (
            run.get("association") != "matched",
            not bool(run.get("reported_issue_count")),
            -(run.get("reported_issue_count") or 0),
            run.get("origin") == "tool_invocation",
            run.get("invocation_status") not in {"failed", "error"},
        ),
    )[:3]
    compact_runs = []
    run_states = {
        "attempted": "已尝试，结果未确认",
        "executed": "有已执行用例结果",
        "reported": "已生成问题报告",
        "not_executed": "没有已执行用例",
        "unknown": "执行情况未知",
    }
    for run in important:
        refs = [reference(ref) for ref in run.get("evidence", [])[:1]]
        compact_runs.append(
            {
                "origin": run.get("origin"),
                "test_type": run.get("test_type"),
                "association": run.get("association"),
                "execution_state": run.get("execution_state"),
                "summary": (
                    f"报告记录 {run['reported_issue_count']} 个问题"
                    if run.get("reported_issue_count") is not None
                    else "执行调用失败，结果未确认"
                    if run.get("invocation_status") in {"failed", "error"}
                    and run.get("execution_state") == "attempted"
                    else run_states.get(run.get("execution_state"), "执行情况未知")
                ),
                "counts": run.get("counts", {}),
                "reported_issue_count": run.get("reported_issue_count"),
                "invocation_status": run.get("invocation_status"),
                "references": [ref for ref in refs if ref],
            }
        )

    platform = {
        key: brief(task[key])
        for key in (
            "title",
            "task_status",
            "task_is_completed",
            "work_mode",
            "worker_mode",
            "task_source",
        )
        if key in task
    }
    classification = task.get("task_classification")
    if isinstance(classification, dict):
        platform["task_classification"] = {
            key: classification[key]
            for key in (
                "category",
                "level_1_category",
                "level_2_category",
                "level_3_category",
                "story_stage",
            )
            if isinstance(classification.get(key), str) and classification[key]
        }
    if isinstance(task.get("related_logic_tasks"), list):
        platform["logic_categories"] = _unique_categories(task["related_logic_tasks"])
    agents = task.get("agents")
    if isinstance(agents, dict):
        platform["agents"] = {
            key: {
                field: brief(agent.get(field), 80)
                for field in ("agent", "model", "thinking_effort")
            }
            for key, agent in agents.items()
            if isinstance(agent, dict)
        }
    dev = task.get("dev_info")
    if isinstance(dev, dict):
        platform["dev_info"] = {key: dev[key] for key in COUNT_KEYS if key in dev}
    metrics = [
        {
            "source": metric.get("source"),
            "tokens_complete": metric.get("tokens_complete"),
            "timing_complete": metric.get("timing_complete"),
            "values": {
                key: metric.get("values", {}).get(key)
                for key in metric.get("values", {})
                if isinstance(metric["values"][key], (int, float))
                or metric["values"][key] is None
            },
        }
        for metric in other.get("metrics", [])[:1]
    ]
    activities = Counter(
        (activity["kind"], activity.get("detail"))
        for activity in other.get("activities", [])
    )
    activity_summary = [
        {"kind": kind, "detail": detail, "count": count}
        for (kind, detail), count in activities.items()
    ]
    skills = sorted(
        {
            item["skill"]
            for item in other.get("skill_events", []) + other.get("activities", [])
            if isinstance(item.get("skill"), str) and item["skill"]
        }
    )
    edit_paths = {
        path
        for activity in other.get("activities", [])
        for path in activity.get("paths", [])
        if isinstance(path, str)
    }
    repository = {
        key: task[key] for key in ("repo_path",) if isinstance(task.get(key), str)
    }
    if "repository_extraction" in other:
        repository["extraction"] = other["repository_extraction"]
    commits = task.get("commit_list")
    if isinstance(commits, list):
        repository["commit_count"] = len(commits)
    dev_metrics = task.get("dev_metrics")
    if isinstance(dev_metrics, dict):
        repository.update(
            {
                key: dev_metrics[key]
                for key in (
                    "with_sdd",
                    "code_repos",
                    "added_lines",
                    "deleted_lines",
                    "changed_files",
                    "changed_dirs",
                    "binary_files",
                    "too_large_files",
                )
                if isinstance(dev_metrics.get(key), (int, float, bool))
            }
        )
    matched_runs = [run for run in runs if run.get("association") == "matched"]
    issue_counts = [
        run["reported_issue_count"]
        for run in matched_runs
        if run.get("reported_issue_count") is not None
    ]
    data_quality = {
        "datasets": {
            name: {"status": entry.get("status"), "complete": entry.get("complete")}
            for name, entry in datasets.items()
            if isinstance(entry, dict)
        },
        "analysis_error_codes": sorted(
            {
                error.get("code", "UNKNOWN")
                for error in result.get("analysis_errors", [])
            }
        ),
        "export_error_codes": sorted(
            {error.get("code", "UNKNOWN") for error in result.get("export_errors", [])}
        ),
    }
    for key in ("export_status", "legacy"):
        if key in other:
            data_quality[key] = other[key]
    coverage = core.get("coverage", {})
    if "rule_search_complete" in coverage:
        data_quality["rule_search_complete"] = coverage["rule_search_complete"]
    test_results = {
        "observed_test_types": sorted(
            {run["test_type"] for run in matched_runs if run.get("test_type")}
        ),
        "execution_states": sorted(
            {
                run["execution_state"]
                for run in matched_runs
                if run.get("execution_state")
            }
        ),
        "association_counts": dict(
            Counter(run.get("association", "unknown") for run in runs)
        ),
        "failed_result_record_count": sum(
            (run.get("counts", {}).get("failed") or 0) > 0 for run in matched_runs
        ),
        "blocked_result_record_count": sum(
            (run.get("counts", {}).get("blocked") or 0) > 0 for run in matched_runs
        ),
        "reported_issue_count_max": max(issue_counts) if issue_counts else None,
    }
    notes = []
    missing = other.get("tool_statistics", {}).get("unparsed_shell_commands", 0)
    if missing:
        notes.append(f"{missing} 次复杂命令未解析，仅影响证据覆盖判断。")
    unbound = sum(run.get("association") != "matched" for run in runs)
    if unbound:
        notes.append(f"{unbound} 条记录归属未确认，未用于准入。")
    if len(runs) > len(compact_runs):
        notes.append(
            f"共识别 {len(runs)} 条测试/审查记录，"
            f"仅展示 {len(compact_runs)} 条关键摘要。"
        )
    codes = admission.get("reason_codes", [])
    level = admission.get("candidate_level", "review")
    decision = admission.get("decision", "hold")
    hit_count = admission.get("evidence_category_count", 0)
    reason_text = [REASONS.get(code, code) for code in codes]
    selected_refs = list(
        dict.fromkeys(
            item["reference"]
            for item in evidence
            if item["matched"] and item.get("reference")
        )
    )
    return {
        "schema_version": "flux.test-admission.v2",
        "task_id": result["task_id"],
        "title": brief(task.get("title", "")),
        "summary": f"{LEVELS[level]} · {DECISIONS[decision]} · "
        f"命中 {hit_count}/5 类证据；" + "；".join(reason_text),
        "analyzer_version": result.get("analyzer_version"),
        "analyzed_at": result.get("analyzed_at"),
        "admission": {
            "candidate_level": level,
            "decision": decision,
            "reason_codes": codes,
            "reasons": reason_text,
            "evidence_category_count": hit_count,
            "matched_evidence_categories": admission.get(
                "matched_evidence_categories", []
            ),
            "evidence_refs": selected_refs if decision == "keep" else [],
        },
        "core": {
            "test_evidence_level": core.get("test_evidence_level", "unknown"),
            "evidence_matches": evidence,
            "test_types": core.get("test_types", []),
            "problem_signal": core.get("problem_signal"),
            "observed_test_run_count": core.get("observed_test_run_count"),
            "test_result_record_count": core.get("test_result_record_count"),
            "runs": compact_runs,
            "runs_total": len(runs),
            "rule_search_complete": core.get("coverage", {}).get(
                "rule_search_complete"
            ),
        },
        "recorded_only": {
            "platform": platform,
            "tool_statistics": other.get("tool_statistics", {}),
            "activities": activity_summary,
            "skills": skills,
            "observed_edit_file_count": len(edit_paths),
            "metrics": metrics,
            "artifact_file_count": len(other.get("artifact_files", [])),
            "versions": [
                {
                    "latest_version_id": value.get("latest_version_id"),
                    "version_count": len(value.get("versions") or []),
                    "comparison_count": len(value.get("comparisons") or []),
                    "has_full_snapshot": value.get("has_full_snapshot"),
                }
                for value in other.get("versions", [])[:1]
            ],
            "repository": repository,
            "test_results": test_results,
            "data_quality": data_quality,
        },
        "references": references,
        "notes": notes,
        "analysis_errors": _errors(result.get("analysis_errors", [])),
        "export_errors": _errors(result.get("export_errors", [])),
    }
