"""Repository labels must preserve source boundaries and never invent use."""

import json

import pytest

from flux_task_service.tagging.models import Event, Evidence
from flux_task_service.tagging.repositories import (
    extract_repositories,
    repository_identity,
)


def document(content, dataset="conversation", pointer=""):
    return {
        "dataset": dataset,
        "reference": {"path": f"{dataset}.json", "pointer": pointer},
        "content": content,
    }


def user(text):
    return {"kind": "user_message", "payload": {"data": {"text": text}}}


def shell(command, output="", code=None, status="succeeded"):
    return Event(
        "tool_call",
        "bash",
        {"command": command},
        output,
        status,
        Evidence("execution", "trace.json", "/steps/0", call_id=command),
        exit_code=code,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("git@github.com:Org/Repo.git", ("Org/Repo", "github.com")),
        ("https://github.com/Org/Repo/tree/main", ("Org/Repo", "github.com")),
        (
            "https://gitlab.com/org/group/repo/-/tree/main",
            ("org/group/repo", "gitlab.com"),
        ),
        ("org/repo", ("org/repo", None)),
        ("/workspace/org/repo", None),
        ("../org/repo", None),
        ("@scope/package", None),
        ("project_name", None),
    ],
)
def test_identity_normalization(value, expected):
    assert repository_identity(value) == expected


def test_multiple_sources_and_purposes_keep_per_evidence_state_and_branch():
    docs = [
        document(
            {
                "items": [
                    {
                        "task": {
                            "task_id": "111",
                            "repo_path": "",
                            "commit_list": [
                                {
                                    "repo_name": "org/main",
                                    "branch": "fix",
                                    "commit_id": "abc",
                                }
                            ],
                        }
                    }
                ]
            },
            "task",
        ),
        document(
            {
                "steps": [
                    user(
                        "目标代码仓库：org/main；参考仓库：org/ref\n"
                        "Skill 仓库：org/skills\n文档输出仓库：org/docs\n"
                        "参考仓库：org/main"
                    )
                ]
            }
        ),
    ]
    result = extract_repositories(docs, [], "111")
    repos = {r["name"]: r for r in result["repositories"]}
    assert set(repos) == {"org/main", "org/ref", "org/skills", "org/docs"}
    assert repos["org/main"]["categories"] == ["commit_metadata", "user_specified"]
    assert repos["org/main"]["purposes"] == ["reference_code", "target_code"]
    assert repos["org/main"]["states"] == ["commit_recorded", "mentioned"]
    assert repos["org/ref"]["purposes"] == ["reference_code"]
    assert repos["org/skills"]["purposes"] == ["skill_knowledge"]
    assert repos["org/docs"]["purposes"] == ["artifact_output"]
    refs = repos["org/main"]["evidence"]
    assert refs[0]["branch"] == "fix"
    assert refs[0]["revision"] == "abc"
    assert all("branch" not in e for e in refs[1:])
    assert refs[0]["pointer"] == "/items/0/task/commit_list/0"


def test_runtime_label_and_legacy_encoded_prompt_are_not_user_text():
    text = (
        "## Worker Runtime Context\n"
        "- 工作目录下当前的仓库列表为：\n"
        "  - ./org/one/ (branch: main)\n"
        "  - ./org/two/ (branch: release/v2)\n"
    )
    docs = [
        document(
            {
                "steps": [
                    {
                        "kind": "user_message",
                        "payload": {
                            "data": {
                                "text": "检查当前项目",
                                "contextSections": [
                                    {"label": "Worker Runtime Context", "text": text}
                                ],
                            }
                        },
                    }
                ],
                "raw": [
                    {
                        "span_type": "agent-run",
                        "input": json.dumps(
                            text + "## Unrelated convention\n仓库：example/ignore"
                        ),
                    }
                ],
            }
        )
    ]
    result = extract_repositories(docs, [], "111")
    assert {r["name"] for r in result["repositories"]} == {"org/one", "org/two"}
    for repo in result["repositories"]:
        assert repo["categories"] == ["runtime_context"]
        assert repo["states"] == ["mounted"]
        assert repo["purposes"] == ["execution_context"]
        assert repo["evidence"][1]["encoding"] == "json-string"
        assert repo["evidence"][1]["pointer"] == "/raw/0/input"


def test_references_and_tool_returned_messages_cannot_become_actual_use():
    doc = document(
        {
            "steps": [
                user(
                    "资料示例：https://github.com/example/demo\n"
                    "依赖 @scope/pkg\n本地 /tmp/org/local"
                ),
                {
                    "kind": "assistant_message",
                    "payload": {"data": {"text": "仓库：org/invented"}},
                },
                {
                    "kind": "tool_call",
                    "result": {
                        "messages": [{"role": "user", "content": "仓库：org/fake"}]
                    },
                },
            ]
        }
    )
    result = extract_repositories([doc], [], "111")
    assert len(result["repositories"]) == 1
    repo = result["repositories"][0]
    assert repo["name"] == "example/demo"
    assert repo["purposes"] == ["unknown"]
    assert repo["states"] == ["mentioned"]


@pytest.mark.parametrize(
    ("event", "state"),
    [
        (
            shell("git clone https://github.com/org/repo.git", "fatal: 403", 128),
            "prepare_failed",
        ),
        (shell("git clone https://github.com/org/repo.git", "", 0), "prepared"),
        (shell("git clone https://github.com/org/repo.git"), "prepare_attempted"),
        (
            shell("git clone https://github.com/org/repo.git; false", "fatal: 403", 1),
            "prepare_attempted",
        ),
    ],
)
def test_clone_success_is_not_inferred_from_invocation_or_compound_status(event, state):
    repo = extract_repositories([], [event], "111")["repositories"][0]
    assert repo["states"] == [state]
    assert repo["purposes"] == ["unknown"]
    assert repo["evidence"][0]["result_excerpt"] == event.output


def test_remote_output_needs_success_and_same_name_on_different_hosts_is_separate():
    good = shell(
        "git -C /workspace/repo remote -v",
        "origin git@github.com:org/repo.git (fetch)",
        0,
    )
    bad = shell("git remote -v", "git@github.com:org/not_verified.git", 1)
    clone = shell("git clone https://gitlab.com/org/repo.git", "", 0)
    repos = extract_repositories([], [good, bad, clone], "111")["repositories"]
    assert len(repos) == 2
    assert {r["platform"] for r in repos} == {"github.com", "gitlab.com"}
    assert repos[0]["states"] == ["remote_verified"]


def test_unknown_sources_and_wrong_task_never_supply_an_identity():
    result = extract_repositories(
        [
            document(
                {
                    "items": [
                        {"task": {"task_id": "222", "repo_path": "org/wrong"}},
                        {
                            "task": {
                                "task_id": "111",
                                "repo_path": "/local/project",
                                "context_project_id": "fp_123",
                            }
                        },
                    ]
                },
                "task",
            )
        ],
        [],
        "111",
    )
    assert result["status"] == "unknown"
    assert result["repositories"] == []
    assert result["unresolved"][0]["reason"] == "local_or_unknown_repo_path"


def test_evidence_pointers_resolve_to_the_actual_source():
    content = {"steps": [user("目标仓库：https://github.com/org/repo.git")]}
    result = extract_repositories([document(content)], [], "111")
    ref = result["repositories"][0]["evidence"][0]
    target = content
    for part in ref["pointer"].split("/")[1:]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    assert ref["excerpt"] in target


def test_structured_user_repo_and_unique_host_merge_preserve_original_platform():
    content = {
        "steps": [
            user(
                {
                    "repo": "org/backend",
                    "purpose": "target_code",
                    "branch": "main",
                }
            ),
            user("参考仓库：https://github.com/org/backend.git"),
        ]
    }
    repos = extract_repositories([document(content)], [], "111")["repositories"]
    assert len(repos) == 1
    assert repos[0]["purposes"] == ["reference_code", "target_code"]
    assert repos[0]["platform"] == "github.com"
    assert repos[0]["identity_resolution"] == "single_observed_host_for_name"
    assert {e["platform"] for e in repos[0]["evidence"]} == {None, "github.com"}


def test_rich_link_list_shares_explicit_purpose_without_url_words_leaking():
    links = "".join(
        f"[link:title=https://code.byted.org/org/{name}, "
        f"url=https://code.byted.org/org/{name}]"
        for name in ("one", "two", "three")
    )
    result = extract_repositories(
        [
            document(
                {
                    "steps": [
                        user(
                            "产品代码仓库地址：" + links + "\n"
                            "更新已有用户手册：https://code.byted.org/docs/manual\n"
                            "不要修改仓库：https://code.byted.org/org/ignored"
                        )
                    ]
                }
            )
        ],
        [],
        "111",
    )
    repos = {r["name"]: r for r in result["repositories"]}
    assert repos["org/three"]["purposes"] == ["target_code"]
    assert repos["docs/manual"]["purposes"] == ["artifact_output"]
    assert repos["org/ignored"]["purposes"] == ["unknown"]


def test_projected_git_arguments_recover_missing_raw_input_without_losing_location():
    doc = document(
        {
            "trace_detail": {
                "data": {
                    "raw": [
                        {
                            "span_type": "tool-call",
                            "name": "tool-call(exec_command)",
                            "trace_id": "t",
                            "span_id": "s",
                            "outcome": "failed",
                        }
                    ],
                    "steps": [
                        {
                            "kind": "tool_call",
                            "status": "failed",
                            "evidence": {"trace_id": "t", "span_id": "s"},
                            "payload": {
                                "data": {
                                    "name": "exec_command",
                                    "parsed_arguments": {
                                        "command": "git ls-remote --heads https://code.byted.org/org/denied.git",
                                    },
                                }
                            },
                            "result": {"output": "Forbidden: 403"},
                        }
                    ],
                }
            }
        }
    )
    doc["reference"]["content_kind"] = "full-trace"
    repos = extract_repositories([doc], [], "111")["repositories"]
    assert len(repos) == 1
    assert repos[0]["states"] == ["access_failed"]
    assert repos[0]["evidence"][0]["pointer"] == "/trace_detail/data/steps/0"


def test_clone_branch_is_bound_to_its_repository_only():
    events = [
        shell("git clone --branch feature/one https://github.com/org/one.git", "", 0),
        shell("git clone https://github.com/org/two.git", "", 0),
    ]
    repos = extract_repositories([], events, "111")["repositories"]
    assert repos[0]["evidence"][0]["branch"] == "feature/one"
    assert "branch" not in repos[1]["evidence"][0]
    remote = shell(
        "git ls-remote https://github.com/org/one.git", "abc403def refs/heads/main", 0
    )
    repo = extract_repositories([], [remote], "111")["repositories"][0]
    assert repo["states"] == ["remote_verified"]
