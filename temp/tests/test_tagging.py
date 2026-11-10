"""Admission checks focus on false positives, provenance and source isolation."""

import json

import pytest

from flux_task_service.export_contract import DATASETS, ContractError
from flux_task_service.tagging import TaggingPolicy, analyze_task
from flux_task_service.tagging.__main__ import main
from flux_task_service.tagging.adapters import deduplicate
from flux_task_service.tagging.commands import classify, command_segments
from flux_task_service.tagging.models import Event, Evidence


def task_dir(tmp_path, identity="111"):
    root = tmp_path / identity
    root.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0",
        "task_id": identity,
        "finalized": True,
        "task_status": "ok",
        "legacy": False,
        "errors": [],
        "datasets": {
            name: {
                "status": "not_requested",
                "complete": None,
                "source": None,
                "scope": {},
                "files": [],
                "errors": [],
                "warnings": [],
            }
            for name in DATASETS
        },
    }
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    return root


def add_dataset(
    root,
    name,
    value,
    *,
    fmt="json-object.v1",
    complete=True,
    status="available",
    path=None,
    role="data",
    kind=None,
):
    path = path or f"renamed-{name}.json"
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(json.dumps(value))
    manifest = json.loads((root / "data-manifest.json").read_text())
    manifest["datasets"][name] = {
        "status": status,
        "complete": complete,
        "source": "test-fixture",
        "scope": {},
        "files": [
            {
                "path": path,
                "format": fmt,
                "pointer": "",
                "role": role,
                "content_kind": kind,
            }
        ],
        "errors": [],
        "warnings": [],
    }
    (root / "data-manifest.json").write_text(json.dumps(manifest))


def tool(command, output="", *, span="s1", status="success", name="bash"):
    return {
        "id": span,
        "kind": "tool_call",
        "title": f"Tool Call: {name}",
        "status": status,
        "payload": {
            "name": name,
            "call_id": "call-" + span,
            "parsed_arguments": {"command": command},
        },
        "result": {"call_id": "call-" + span, "output": output},
        "evidence": {"trace_id": "trace", "span_id": span},
        "children": [],
    }


def execution(root, steps, *, complete=True, meta=None):
    add_dataset(
        root,
        "execution",
        {
            "schema_version": "trace-read-model.v2",
            "meta": meta or {},
            "sessions": [
                {"id": "session-1", "turns": [{"id": "turn-1", "steps": steps}]}
            ],
        },
        fmt="trace-read-model.v2",
        complete=complete,
        status="available" if complete else "partial",
    )


def report(root, *, task_id="111", failed=1, passed=2, run_id="r1"):
    data = {
        "schema": "unit_test/report@2",
        "run": {"run_id": run_id, "flux_task_id": task_id},
        "summary": {
            "passed": passed,
            "failed": failed,
            "blocked": 0,
            "skipped": 0,
            "not_run": 0,
            "issues": {"total": failed},
        },
    }
    add_dataset(root, "artifacts", data, fmt="bytes.v1", role="artifact")


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "/repo/.venv/bin/pytest tests",
        "python3 -m pytest",
        "uv run --locked python -m pytest",
        "env MODE=test pytest -q",
        "bash -lc 'go test ./...'",
        "npx --yes vitest run",
        "npm run test",
        "node --test tests/a.js",
        "pnpm exec playwright test",
        "cargo test",
    ],
)
def test_known_executable_test_commands(command):
    matches = [
        classify(tokens, TaggingPolicy()) for tokens in command_segments(command)
    ]
    assert any(match and match[0] == "test" for match in matches)


@pytest.mark.parametrize(
    "command",
    [
        "echo 'pytest -q'",
        "cat sys-skills/bits-unit-test-gen/SKILL.md",
        "rg 'pytest' README.md",
        "pytest --collect-only",
        "pytest --help",
        "npm run typecheck",
        "emo build app",
        "node -e 'console.log(\"pytest\")'",
        "python - <<'PY'\npytest = 'example'\nPY",
        "echo $(printf 'a; pytest')",
        "cat <<EOF\npytest -q\nEOF",
        "pytest '\"",
    ],
)
def test_examples_help_checks_and_heredocs_do_not_become_tests(command):
    matches = [
        classify(tokens, TaggingPolicy()) for tokens in command_segments(command)
    ]
    assert not any(match and match[0] == "test" for match in matches)


def test_failure_does_not_upgrade_two_evidence_categories_to_priority(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q", "=== 2 passed, 1 failed in 0.30s ===")])
    result = analyze_task(root)
    assert result["admission"]["decision"] == "keep"
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["admission"]["evidence_category_count"] == 2
    assert result["core"]["problem_signal"] is True
    run = result["core"]["runs"][0]
    assert run["counts"]["failed"] == 1
    assert run["counts"]["blocked"] is None
    assert run["references"]
    assert result["references"][0]["path"] == "renamed-execution.json"


def test_success_only_and_priority_policy(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q", "2 passed in 0.30s")])
    assert analyze_task(root)["admission"]["candidate_level"] == "ordinary_candidate"
    result = analyze_task(root, policy=TaggingPolicy(require_problem_signal=True))
    assert result["admission"]["decision"] == "exclude"


def test_failed_test_invocation_alone_requires_review(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q", "No module named pytest", status="failed")])
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["admission"]["candidate_level"] == "review"
    assert result["core"]["test_evidence_level"] == "invocation_observed"
    assert result["core"]["problem_signal"] is False
    assert result["core"]["runs"][0]["invocation_status"] == "failed"


def test_skill_read_lint_build_and_platform_defects_do_not_admit(tmp_path):
    root = task_dir(tmp_path)
    execution(
        root,
        [
            tool(
                "cat sys-skills/bits-unit-test-gen/SKILL.md; "
                "npm run lint; npm run build"
            )
        ],
    )
    add_dataset(
        root,
        "task",
        {
            "items": [
                {
                    "task_id": "111",
                    "task": {
                        "task_id": "111",
                        "dev_info": '{"defect_found_count": 99}',
                    },
                }
            ]
        },
    )
    result = analyze_task(root)
    assert result["core"]["test_evidence_level"] == "setup_only"
    assert result["admission"]["decision"] == "exclude"
    assert result["recorded_only"]["platform"]["dev_info"]["defect_found_count"] == 99


def test_error_text_in_non_test_output_is_not_a_problem_signal(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("cat report.md", "=== 9 failed in 0.20s ===")])
    assert analyze_task(root)["core"]["runs"] == []


def test_duplicate_execution_and_conversation_is_counted_once(tmp_path):
    root = task_dir(tmp_path)
    step = tool("pytest -q", "1 passed in 0.2s")
    execution(root, [step])
    add_dataset(
        root,
        "conversation",
        {
            "trace_detail": {
                "data": {
                    "turns": [{"id": "turn-1", "steps": [step]}],
                }
            }
        },
        kind="full-trace",
    )
    result = analyze_task(root)
    assert result["recorded_only"]["tool_statistics"]["tool_call_count"] == 1
    assert result["core"]["observed_test_run_count"] == 1


def test_report_of_other_task_is_not_admitted(tmp_path):
    root = task_dir(tmp_path)
    report(root, task_id="222")
    result = analyze_task(root)
    assert result["core"]["runs"][0]["association"] == "mismatch"
    assert result["admission"]["decision"] == "hold"
    assert result["core"]["problem_signal"] is None
    assert result["admission"]["evidence_refs"] == []


def test_matched_report_and_zero_are_not_unknown(tmp_path):
    root = task_dir(tmp_path)
    report(root, failed=0)
    result = analyze_task(root)
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["core"]["runs"][0]["counts"]["failed"] == 0


def test_partial_with_complete_true_and_recovery_error_can_admit(tmp_path):
    root = task_dir(tmp_path)
    report(root)
    manifest = json.loads((root / "data-manifest.json").read_text())
    manifest["datasets"]["artifacts"].update(
        status="partial",
        errors=[{"code": "RECOVERED_FETCH_ERROR", "message": "fallback used"}],
    )
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    assert analyze_task(root)["admission"]["decision"] == "keep"


def test_unrelated_invalid_metrics_do_not_block_test_result(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest", "1 failed in 0.2s")])
    add_dataset(root, "metrics", {"values": {}}, fmt="flux.metrics.v1")
    result = analyze_task(root)
    assert result["admission"]["decision"] == "keep"
    assert result["analysis_errors"][0]["dataset"] == "metrics"
    assert len(result["admission"]["evidence_refs"]) == 1


@pytest.mark.parametrize(
    "complete,meta",
    [
        (False, {}),
        (True, {"projection": {"mode": "metadata_only"}}),
    ],
)
def test_missing_match_with_insufficient_coverage_is_not_exclusion(
    tmp_path, complete, meta
):
    root = task_dir(tmp_path)
    execution(root, [], complete=complete, meta=meta)
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["core"]["test_evidence_level"] == "unknown"


def test_compound_command_cannot_assign_shell_success_to_each_test(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q; echo done", "1 failed in 0.2s\ndone")])
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["admission"]["candidate_level"] == "review"
    assert result["core"]["runs"][0]["counts"]["failed"] is None


def test_gui_blocked_only_is_not_executed(tmp_path):
    root = task_dir(tmp_path)
    add_dataset(
        root,
        "gui",
        {
            "items": [
                {
                    "task_id": "111",
                    "run_id": "g1",
                    "case_result": [{"status": "BLOCKED"}],
                }
            ]
        },
    )
    result = analyze_task(root)
    assert result["admission"]["decision"] == "keep"
    assert result["core"]["observed_test_run_count"] == 0
    assert result["core"]["runs"][0]["counts"]["blocked"] == 1


def test_gui_failure_plus_blocker_is_kept(tmp_path):
    root = task_dir(tmp_path)
    add_dataset(
        root,
        "gui",
        {
            "items": [
                {
                    "task_id": "111",
                    "case_result": [
                        {"status": "FAIL"},
                        {"status": "BLOCKED"},
                    ],
                }
            ]
        },
    )
    result = analyze_task(root)
    assert result["admission"]["candidate_level"] == "ordinary_candidate"


def test_configured_test_entry_and_allowed_types(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("emo run test:api")])
    policy = TaggingPolicy.model_validate(
        {
            "command_rules": [
                {"prefix": ["emo", "run", "test:api"], "test_type": "api"},
            ]
        }
    )
    assert analyze_task(root, policy=policy)["core"]["test_types"] == ["api"]
    assert analyze_task(root)["core"]["test_types"] == []
    report(root)
    result = analyze_task(root, policy=TaggingPolicy(allowed_test_types=["gui"]))
    assert result["admission"]["decision"] == "exclude"


def test_unfinalized_manifest_is_error_not_task_cancellation(tmp_path):
    root = task_dir(tmp_path)
    manifest = json.loads((root / "data-manifest.json").read_text())
    manifest.update(finalized=False, task_status="cancelled")
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError, match="unfinished"):
        analyze_task(root)


def test_cli_writes_separate_selection_index_and_leaves_input_unchanged(tmp_path):
    root = task_dir(tmp_path / "inputs")
    report(root)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    output = tmp_path / "output"
    assert main([str(root), "--output", str(output)]) == 0
    selected = json.loads((output / "selected-tasks.json").read_text())
    assert selected[0]["task_id"] == "111"
    index = json.loads((output / "index.json").read_text())
    detail = json.loads((output / "tasks/111.json").read_text())
    assert index["records"][0]["tags"] == detail["recorded_only"]
    assert selected[0]["tags"] == index["records"][0]["tags"]
    assert (
        index["records"][0]["tags"]["test_results"]["failed_result_record_count"] == 1
    )
    assert all(p.read_bytes() == content for p, content in before.items())
    assert main([str(root), "--output", str(root / "tags")]) == 1


def test_empty_case_result_is_recorded_without_rejecting_the_test_task(tmp_path):
    root = task_dir(tmp_path)
    report(root, failed=0, passed=0)
    result = analyze_task(root)
    assert result["admission"]["decision"] == "keep"
    assert result["core"]["observed_test_run_count"] == 0


def test_auxiliary_metrics_error_does_not_change_negative_admission(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("npm run lint")])
    add_dataset(root, "metrics", {"values": {}}, fmt="flux.metrics.v1")
    result = analyze_task(root)
    assert result["admission"]["decision"] == "exclude"
    assert result["analysis_errors"][0]["dataset"] == "metrics"


def test_dynamic_shell_is_recorded_as_incomplete_rule_coverage(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("python - <<'PY'\nprint('pytest')\nPY")])
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["recorded_only"]["tool_statistics"]["unparsed_shell_commands"] == 1


def test_unknown_case_counts_do_not_reject_the_test_task(tmp_path):
    root = task_dir(tmp_path)
    report(root, passed="3", failed=None)
    result = analyze_task(root)
    assert result["admission"]["decision"] == "keep"
    assert result["core"]["runs"][0]["counts"]["passed"] is None


def test_fornax_span_and_body_view_merge_using_trace_and_span_ids(tmp_path):
    root = task_dir(tmp_path)
    span = {
        "name": "tool-call(bash)",
        "span_type": "tool-call",
        "span_id": "s1",
        "trace_id": "trace",
        "outcome": "success",
        "input": '{"arguments":"{\\"command\\":\\"pytest -q\\"}"}',
        "output": '"1 passed in 0.2s"',
    }
    add_dataset(root, "execution", [span], fmt="flux.normalized-spans.v1")
    add_dataset(
        root,
        "conversation",
        {
            "trace_detail": {
                "data": {
                    "raw": [span],
                }
            }
        },
        kind="full-trace",
    )
    result = analyze_task(root)
    assert result["recorded_only"]["tool_statistics"]["tool_call_count"] == 1
    assert result["admission"]["decision"] == "keep"


def test_unsupported_internal_shape_does_not_crash_batch(tmp_path):
    root = task_dir(tmp_path)
    add_dataset(
        root,
        "execution",
        {"schema_version": "trace-read-model.v2", "sessions": ["not-an-object"]},
        fmt="trace-read-model.v2",
    )
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["analysis_errors"][0]["code"] == "UNSUPPORTED_EVENT_SHAPE"


def test_reused_call_ids_in_different_sessions_are_not_merged():
    events = [
        Event(
            "tool_call",
            "bash",
            {"command": "pytest"},
            "",
            "success",
            Evidence(
                "execution",
                "trace.json",
                f"/{i}",
                session_id=session,
                call_id="same-call",
            ),
        )
        for i, session in enumerate(["session-a", "session-b"])
    ]
    assert len(deduplicate(events)) == 2


def test_narration_reference_is_not_a_duplicate_model_invocation():
    ref = Evidence("execution", "trace.json", "/1", trace_id="trace", span_id="model")
    events = [
        Event(kind, "", None, None, "success", ref)
        for kind in ["agent_narration", "reasoning", "model_call"]
    ]
    assert len(deduplicate(events)) == 3


@pytest.mark.parametrize(
    "command",
    [
        "GOMAXPROCS=1 timeout 180s go test -p 1 ./pkg",
        "timeout --signal TERM --kill-after 5s 180s go test ./pkg",
        "timeout --foreground 10s bash -lc 'pytest -q'",
    ],
)
def test_timeout_wrapper_does_not_hide_test_invocation(tmp_path, command):
    root = task_dir(tmp_path)
    execution(root, [tool(command, "dependency download failed", status="failed")])
    result = analyze_task(root)
    assert result["core"]["test_evidence_level"] == "invocation_observed"
    assert result["admission"]["decision"] == "hold"


def test_platform_test_classification_is_explicit_scenario_evidence(tmp_path):
    root = task_dir(tmp_path)
    add_dataset(
        root,
        "task",
        {
            "items": [
                {
                    "task_id": "111",
                    "task": {
                        "task_id": "111",
                        "related_logic_tasks": json.dumps(
                            [
                                {
                                    "level_1_category": "测试",
                                    "level_2_category": "完整测试",
                                }
                            ]
                        ),
                    },
                }
            ]
        },
    )
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["core"]["test_evidence_level"] == "scenario_reported"
    assert result["core"]["observed_test_run_count"] == 0
    assert next(
        item
        for item in result["core"]["evidence_matches"]
        if item["category"] == "platform_test_scenario"
    )["matched"]


def test_code_guard_jsonl_binds_to_observed_report_generation(tmp_path):
    root = task_dir(tmp_path)
    execution(
        root,
        [
            tool(
                "python3 /skills/bits-code-guard-doubao/scripts/generate_report.py "
                "final.json --report-dir artifacts/review/run1"
            )
        ],
    )
    path = "artifacts/files/artifacts/review/run1/report.jsonl"
    add_dataset(root, "artifacts", {}, fmt="bytes.v1", role="artifact", path=path)
    (root / path).write_text(
        json.dumps(
            {
                "schema_version": "0.0.0-bits-code-guard",
                "source": "bits-code-guard",
                "record_type": "capability_summary",
                "payload": {
                    "status": "success",
                    "finding_p0": 0,
                    "finding_p1": 1,
                    "finding_p2": 1,
                },
            }
        )
        + "\n"
    )
    (root / "downloads.json").write_text(
        json.dumps(
            [
                {
                    "path": "artifacts/review/run1/report.jsonl",
                    "status": "ok",
                    "file": f"tasks/111/{path}",
                }
            ]
        )
    )
    manifest = json.loads((root / "data-manifest.json").read_text())
    manifest["datasets"]["artifacts"]["files"].append(
        {
            "path": "downloads.json",
            "format": "flux.downloads.v1",
            "pointer": "",
            "role": "download-index",
        }
    )
    (root / "data-manifest.json").write_text(json.dumps(manifest))
    result = analyze_task(root)
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["admission"]["evidence_category_count"] == 2
    report_record = next(
        run
        for run in result["core"]["runs"]
        if run["origin"] == "bits-code-guard-report.v1"
    )
    assert report_record["reported_issue_count"] == 2
    assert report_record["association"] == "matched"
    assert report_record["counts"]["failed"] is None
    assert result["core"]["observed_test_run_count"] == 0


def platform_signal(root):
    add_dataset(
        root,
        "task",
        {
            "items": [
                {
                    "task_id": root.name,
                    "task": {
                        "task_id": root.name,
                        "related_logic_tasks": [
                            {
                                "level_1_category": "测试",
                                "level_2_category": "完整测试",
                            },
                            {
                                "level_1_category": "测试",
                                "level_2_category": "单元测试",
                            },
                        ],
                    },
                }
            ]
        },
    )


def skill_call(span="skill-1"):
    step = tool("", span=span, name="Skill")
    step["payload"]["parsed_arguments"] = {"skill": "bits-unit-test-gen"}
    return step


def test_two_supporting_categories_still_require_review(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q")])
    platform_signal(root)
    result = analyze_task(root)
    assert result["admission"]["decision"] == "hold"
    assert result["admission"]["evidence_category_count"] == 2
    assert set(result["admission"]["matched_evidence_categories"]) == {
        "test_execution_call",
        "platform_test_scenario",
    }


def test_skill_call_alone_is_ordinary_candidate(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [skill_call()])
    result = analyze_task(root)
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["admission"]["decision"] == "keep"
    assert result["admission"]["matched_evidence_categories"] == [
        "test_skill_invocation"
    ]


def test_review_execution_alone_is_one_strong_category(tmp_path):
    root = task_dir(tmp_path)
    execution(
        root,
        [
            tool(
                f"python3 /skills/bits-code-guard-doubao/scripts/{script}.py",
                span=script,
            )
            for script in ["start", "finish", "generate_report"]
        ],
    )
    result = analyze_task(root)
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["admission"]["evidence_category_count"] == 1
    assert result["admission"]["matched_evidence_categories"] == [
        "defect_review_execution"
    ]


def test_three_categories_are_priority_without_problem_signal(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest -q", "2 passed in 0.2s"), skill_call()])
    result = analyze_task(root)
    assert result["core"]["problem_signal"] is False
    assert result["admission"]["evidence_category_count"] == 3
    assert result["admission"]["candidate_level"] == "priority_candidate"
    assert result["admission"]["decision"] == "keep"


def test_all_five_categories_are_counted_once(tmp_path):
    root = task_dir(tmp_path)
    execution(
        root,
        [
            tool("pytest", "2 passed in 0.2s"),
            skill_call(),
            tool(
                "python3 /skills/bits-code-guard-doubao/scripts/start.py", span="review"
            ),
        ],
    )
    platform_signal(root)
    result = analyze_task(root)
    assert result["admission"]["evidence_category_count"] == 5
    assert result["admission"]["candidate_level"] == "priority_candidate"


def test_repeated_skill_calls_do_not_upgrade_one_category(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [skill_call(f"skill-{i}") for i in range(4)])
    result = analyze_task(root)
    assert result["admission"]["evidence_category_count"] == 1
    assert result["admission"]["candidate_level"] == "ordinary_candidate"


def test_priority_only_is_category_based_not_problem_based(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest", "1 failed in 0.2s")])
    result = analyze_task(root, policy=TaggingPolicy(priority_only=True))
    assert result["core"]["problem_signal"] is True
    assert result["admission"]["candidate_level"] == "ordinary_candidate"
    assert result["admission"]["decision"] == "exclude"
    platform_signal(root)
    result = analyze_task(root, policy=TaggingPolicy(priority_only=True))
    assert result["admission"]["candidate_level"] == "priority_candidate"
    assert result["admission"]["decision"] == "keep"


def test_disallowed_skill_type_does_not_count_toward_priority(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [skill_call()])
    result = analyze_task(root, policy=TaggingPolicy(allowed_test_types=["gui"]))
    assert result["admission"]["evidence_category_count"] == 0
    assert result["admission"]["decision"] == "hold"


def test_repository_labels_survive_cli_without_changing_admission(tmp_path):
    root = task_dir(tmp_path)
    execution(root, [tool("pytest", "1 failed in 0.2s")])
    before = analyze_task(root)
    add_dataset(
        root,
        "task",
        {
            "items": [
                {
                    "task": {
                        "task_id": "111",
                        "repo_path": "",
                        "commit_list": [
                            {
                                "repo_name": "org/backend",
                                "branch": "feature/one",
                            }
                        ],
                    }
                }
            ]
        },
    )
    after = analyze_task(root)
    assert before["admission"] == after["admission"]
    output = tmp_path / "tagged"
    assert main([str(root), "--output", str(output)]) == 0
    detail = json.loads((output / "tasks/111.json").read_text())
    index = json.loads((output / "index.json").read_text())
    selected = json.loads((output / "selected-tasks.json").read_text())
    repository = detail["recorded_only"]["repository"]
    assert repository["repo_path"] == ""
    assert repository["extraction"]["repositories"][0]["name"] == "org/backend"
    assert index["records"][0]["tags"]["repository"] == repository
    assert selected[0]["tags"]["repository"] == repository
