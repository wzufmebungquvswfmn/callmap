import json

from flux_task_service.tagging.presentation import compact_result


def test_large_input_is_compressed_without_changing_admission():
    admission = {
        "decision": "keep",
        "candidate_level": "priority_candidate",
        "reason_codes": ["THREE_OR_MORE_EVIDENCE_CATEGORIES"],
        "evidence_category_count": 3,
        "matched_evidence_categories": [
            "test_report",
            "test_execution_call",
            "test_skill_invocation",
        ],
    }
    source = {"path": "report.json", "pointer": "/findings"}
    run = {
        "origin": "report",
        "association": "matched",
        "execution_state": "reported",
        "reported_issue_count": 2,
        "counts": {"failed": 0, "blocked": None},
        "evidence": [source] * 100,
        "command": "secret verbose log" * 1000,
    }
    original = {
        "task_id": "111",
        "admission": admission,
        "core": {
            "runs": [dict(run, reported_issue_count=i) for i in range(20)],
            "evidence_matches": [
                {
                    "category": "test_report",
                    "label": "报告",
                    "matched": True,
                    "sources": [source] * 100,
                }
            ],
            "coverage": {"rule_search_complete": True, "scope": "x" * 100000},
        },
        "datasets": {"raw": "x" * 100000},
        "recorded_only": {
            "platform": {"title": "报告测试", "commit_list": "x" * 100000}
        },
    }
    result = compact_result(original)
    for key, value in admission.items():
        assert result["admission"][key] == value
    assert result["core"]["runs_total"] == 20
    assert len(result["core"]["runs"]) == 3
    assert result["core"]["runs"][0]["reported_issue_count"] == 19
    assert result["core"]["runs"][0]["counts"] == {"failed": 0, "blocked": None}
    assert len(result["references"]) == 1
    assert result["admission"]["evidence_refs"] == ["E1"]
    assert len(json.dumps(result).encode()) < 5000
    assert "secret verbose log" not in json.dumps(result)
    assert len(original["core"]["runs"]) == 20


def test_reference_limit_keeps_valid_navigation_and_hold_is_empty():
    result = compact_result(
        {
            "task_id": "111",
            "admission": {"decision": "hold", "candidate_level": "review"},
            "core": {
                "evidence_matches": [
                    {
                        "category": f"category-{i}",
                        "label": "证据",
                        "matched": True,
                        "sources": [{"path": f"file-{i}.json", "pointer": "/0"}],
                    }
                    for i in range(10)
                ]
            },
        }
    )
    ids = {ref["id"] for ref in result["references"]}
    assert len(ids) == 8
    assert all(
        item.get("reference", "E1") in ids
        for item in result["core"]["evidence_matches"]
    )
    assert result["admission"]["evidence_refs"] == []


def test_auxiliary_labels_survive_compression_and_cover_omitted_runs():
    admission = {"decision": "hold", "candidate_level": "review"}
    segment = {
        "level_1_category": "测试",
        "level_2_category": "完整测试",
        "level_3_category": "接口测试",
        "story_stage": "验收",
        "segment_messages": 10000,
    }
    runs = [
        {
            "origin": "report",
            "association": "matched",
            "test_type": "other",
            "execution_state": "reported",
            "reported_issue_count": 10,
            "counts": {},
        }
        for _ in range(3)
    ]
    runs.append(
        {
            "association": "matched",
            "test_type": "unit",
            "execution_state": "executed",
            "counts": {"failed": 1, "blocked": 2},
        }
    )
    result = compact_result(
        {
            "task_id": "111",
            "admission": admission,
            "core": {"runs": runs, "coverage": {"rule_search_complete": False}},
            "datasets": {
                "execution": {
                    "status": "partial",
                    "complete": False,
                    "scope": {"verbose": "x" * 100000},
                }
            },
            "recorded_only": {
                "legacy": False,
                "export_status": "ok",
                "platform": {
                    "task_classification": {
                        "category": "代码修改",
                        "reason": "x" * 10000,
                    },
                    "related_logic_tasks": [
                        segment,
                        segment,
                        {"level_1_category": "开发"},
                    ],
                    "worker_mode": "agent",
                    "repo_path": "org/repo",
                    "commit_list": ["a", "b"],
                    "dev_metrics": {"changed_files": 0, "with_sdd": False},
                },
                "activities": [
                    {"kind": "test_skill_document_read", "skill": "bits-code-guard"},
                    {"kind": "test_skill_invocation", "skill": "bits-unit-test-gen"},
                    {"kind": "check", "detail": "lint"},
                    {"kind": "file_edit", "paths": ["a.py", "a.py"]},
                ],
                "metrics": [
                    {
                        "values": {
                            "input_tokens": 10,
                            "output_tokens": 0,
                            "cached_tokens": None,
                            "verbose": "x" * 10000,
                        }
                    }
                ],
            },
        }
    )
    tags = result["recorded_only"]
    assert result["admission"]["decision"] == admission["decision"]
    assert tags["platform"]["task_classification"] == {"category": "代码修改"}
    assert tags["platform"]["logic_categories"] == [
        {
            key: segment[key]
            for key in (
                "level_1_category",
                "level_2_category",
                "level_3_category",
                "story_stage",
            )
        },
        {"level_1_category": "开发"},
    ]
    assert tags["platform"]["worker_mode"] == "agent"
    assert tags["skills"] == ["bits-code-guard", "bits-unit-test-gen"]
    assert tags["observed_edit_file_count"] == 1
    assert tags["repository"] == {
        "repo_path": "org/repo",
        "commit_count": 2,
        "changed_files": 0,
        "with_sdd": False,
    }
    assert tags["test_results"]["observed_test_types"] == ["other", "unit"]
    assert tags["test_results"]["failed_result_record_count"] == 1
    assert tags["test_results"]["blocked_result_record_count"] == 1
    assert tags["data_quality"]["datasets"]["execution"] == {
        "status": "partial",
        "complete": False,
    }
    assert tags["metrics"][0]["values"] == {
        "input_tokens": 10,
        "output_tokens": 0,
        "cached_tokens": None,
    }
    assert len(json.dumps(result)) < 6000
