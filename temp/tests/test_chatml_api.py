"""HTTP ChatML conversion reads persisted exports without upstream requests."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.config import Settings
from flux_task_service.export_contract import build_manifest

JOB_ID = "a" * 32
TASK_ID = "111"
URL = f"/v1/chatml/{JOB_ID}/{TASK_ID}"
AUTH = {"Authorization": "Bearer test-api-key"}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def exported_task(tmp_path):
    root = tmp_path / JOB_ID / "tasks" / TASK_ID
    results = {
        "conversation": {
            "status": "ok",
            "source": "flux_insight_openapi",
            "file": f"tasks/{TASK_ID}/conversation.json",
        }
    }
    write_json(
        tmp_path / JOB_ID / "job.json",
        {
            "job_id": JOB_ID,
            "status": "completed",
            "components": ["conversation"],
            "tasks": {TASK_ID: {"status": "ok", "components": results}},
        },
    )
    write_json(
        root / "conversation/full-trace.json",
        {
            "trace_detail": {
                "meta": {"complete": True},
                "data": {
                    "messages": [
                        {"role": "system", "content": "系统提示"},
                        {"role": "user", "content": "用户提示\n下一行"},
                        {
                            "role": "assistant",
                            "reasoning_content": "内部推理",
                            "content": "模型回复",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "Bash",
                                        "arguments": '{"cmd":"pwd"}',
                                    },
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "content": "工具结果",
                            "tool_call_id": "call_1",
                        },
                    ]
                },
            }
        },
    )
    write_json(
        root / "conversation.json",
        {
            "schema_version": "1.0",
            "source": "flux_insight_openapi",
            "complete": True,
            "content_available": True,
            "errors": [],
            "files": [f"tasks/{TASK_ID}/conversation/full-trace.json"],
        },
    )
    manifest = build_manifest(
        root,
        TASK_ID,
        ["conversation"],
        results,
        finalized=True,
        task_status="ok",
        raw_requested=False,
    )
    write_json(root / "data-manifest.json", manifest.model_dump())
    return root


@pytest.fixture
def client(tmp_path, exported_task):
    def unexpected_upstream(request):
        raise AssertionError(f"ChatML must not call upstream: {request.url.path}")

    settings = Settings(
        _env_file=None, export_dir=tmp_path, api_key=SecretStr("test-api-key")
    )
    with TestClient(
        create_app(settings, transport=httpx.MockTransport(unexpected_upstream))
    ) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("query", "count", "included", "excluded"),
    [
        ("", 6, ["系统提示", "用户提示", "模型回复", "内部推理", "工具结果"], []),
        ("?mode=user", 1, ["用户提示\n下一行"], ["模型回复", "内部推理"]),
        (
            "?mode=assistant",
            1,
            ["模型回复"],
            ["系统提示", "用户提示", "内部推理", "工具结果", 'to="Bash"'],
        ),
        ("?mode=user&visible_escapes=true", 1, ["用户提示\\n下一行"], []),
        (
            "?exclude_role=tool",
            5,
            ["系统提示", "用户提示", "模型回复", "内部推理", 'to="Bash"'],
            ["工具结果", "<|im_start|>tool"],
        ),
        (
            "?mode=user&mode=assistant",
            2,
            ["用户提示", "模型回复"],
            ["系统提示", "内部推理", "工具结果", 'to="Bash"'],
        ),
        (
            "?exclude_role=tool&exclude_role=system",
            4,
            ["用户提示", "模型回复", "内部推理"],
            ["系统提示", "工具结果"],
        ),
        (
            "?mode=user&mode=assistant&mode=user&exclude_role=assistant",
            1,
            ["用户提示"],
            ["模型回复", "内部推理", "工具结果"],
        ),
        ("?mode=all&mode=user&exclude_role=tool", 5, ["内部推理"], ["工具结果"]),
    ],
)
def test_chatml_text_modes(client, query, count, included, excluded):
    response = client.get(URL + query, headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-chatml-messages"] == str(count)
    assert response.headers["x-chatml-source-messages"] == "6"
    assert response.headers["x-chatml-warnings"] == "0"
    assert response.text.count("<|im_start|>") == count
    for text in included:
        assert text in response.text
    for text in excluded:
        assert text not in response.text


def test_chatml_requires_authorization(client):
    response = client.get(URL)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    "query",
    [
        "?mode=invalid",
        "?visible_escapes=invalid",
        "?exclude_role=invalid",
        "?mode=user&mode=invalid",
        "?exclude_role=tool&exclude_role=invalid",
    ],
)
def test_chatml_rejects_invalid_parameters(client, query):
    response = client.get(URL + query, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize(
    ("url", "status", "code"),
    [
        (f"/v1/chatml/invalid/{TASK_ID}", 404, "EXPORT_NOT_FOUND"),
        (f"/v1/chatml/{'b' * 32}/{TASK_ID}", 404, "EXPORT_NOT_FOUND"),
        (f"/v1/chatml/{JOB_ID}/222", 404, "EXPORT_TASK_NOT_FOUND"),
        (f"/v1/chatml/{JOB_ID}/not-numeric", 422, "INVALID_QUERY"),
    ],
)
def test_chatml_checks_export_and_task_identity(client, url, status, code):
    response = client.get(url, headers=AUTH)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_chatml_unfinished_manifest(client, exported_task):
    path = exported_task / "data-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["finalized"] = False
    write_json(path, manifest)
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EXPORT_NOT_READY"


def test_chatml_missing_document_is_public_error(client, exported_task):
    (exported_task / "conversation/full-trace.json").unlink()
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHATML_UNAVAILABLE"
    assert str(exported_task) not in response.text


def test_chatml_legacy_requires_explicit_opt_in(client, exported_task):
    (exported_task / "data-manifest.json").unlink()
    assert client.get(URL, headers=AUTH).status_code == 409
    response = client.get(URL + "?allow_legacy=true&mode=user", headers=AUTH)
    assert response.status_code == 200
    assert "用户提示" in response.text


def test_chatml_empty_selection(client, exported_task):
    path = exported_task / "conversation/full-trace.json"
    data = json.loads(path.read_text())
    data["trace_detail"]["data"]["messages"] = [
        {"role": "system", "content": "系统提示"}
    ]
    write_json(path, data)
    response = client.get(URL + "?mode=user", headers=AUTH)
    assert response.status_code == 200
    assert response.text == ""
    assert response.headers["x-chatml-messages"] == "0"


def test_chatml_rejects_task_directory_symlink(client, exported_task, tmp_path):
    outside = tmp_path / "outside" / TASK_ID
    outside.parent.mkdir()
    exported_task.rename(outside)
    exported_task.symlink_to(outside, target_is_directory=True)
    response = client.get(URL, headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EXPORT_TASK_NOT_FOUND"


def test_chatml_openapi_exposes_query_parameters_and_plain_text(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/v1/chatml/{job_id}/{task_id}"
    ]["get"]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert parameters["mode"]["in"] == "query"
    array_schema = parameters["mode"]["schema"]["anyOf"][0]
    assert array_schema["type"] == "array"
    assert array_schema["items"]["enum"] == ["all", "user", "assistant"]
    assert parameters["exclude_role"]["in"] == "query"
    assert "requestBody" not in operation
    assert "text/plain" in operation["responses"]["200"]["content"]
