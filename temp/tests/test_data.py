"""Contract checks for source selection, artifacts, exports and failure isolation."""

import asyncio
import gzip
import hashlib
import io
import json
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.client import OpenAPIClient
from flux_task_service.config import Settings
from flux_task_service.export_contract import TaskDataReader
from flux_task_service.exports import ExportManager
from flux_task_service.metrics import _iso_to_ms
from flux_task_service.models import ExportQuery, TraceQuery
from flux_task_service.resources import ResourceService
from flux_task_service.service import TaskService


def config(tmp_path, **values):
    return Settings(
        _env_file=None,
        **(
            {
                "service_secret": SecretStr("service-secret"),
                "fornax_jwt": SecretStr("personal-token"),
                "export_dir": tmp_path,
                "max_retries": 0,
                "requests_per_second": 10,
            }
            | values
        ),
    )


def ok(data):
    return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})


def trace_data(*, complete=True):
    return {
        "schema_version": "trace-read-model.v2",
        "meta": {"complete": complete, "counts": {"spans": 1, "model_calls": 2}},
        "raw_spans": [
            {
                "span_id": "s1",
                "operation": "model-call",
                "payload": {"input": "原始输入"},
            }
        ],
        "summary": {
            "token_usage": {
                "input": 100,
                "output": 20,
                "cached_input": 30,
                "known": True,
                "total_known": True,
            }
        },
        "sessions": [
            {
                "role": "primary",
                "turns": [
                    {
                        "timing": {"started_at": 1000, "ended_at": 2000},
                        "model_calls": [],
                    },
                    {
                        "timing": {"started_at": 1500, "ended_at": 2500},
                        "model_calls": [],
                    },
                    {
                        "timing": {"started_at": 3000, "ended_at": 4000},
                        "model_calls": [],
                    },
                ],
            }
        ],
    }


def fornax_span(span_id="f1", name="model-call"):
    return {
        "span_id": span_id,
        "trace_id": "t1",
        "span_name": name,
        "started_at": 1000,
        "duration": 100,
        "custom_tags": {"input_tokens": "10", "output_tokens": "2"},
        "input": '{"original": true}',
    }


def transport(handler, requests=None):
    async def route(request):
        if requests is not None:
            requests.append(request)
        if request.url.path == "/auth/api/v1/jwt":
            assert request.headers["Authorization"] == "Bearer service-secret"
            return httpx.Response(200, headers={"X-Jwt-Token": "service-token"})
        assert request.url.path.startswith(
            ("/openapi/v1/", "/open-api/observability/")
        ), "No Web API calls"
        if request.url.path.startswith("/openapi/"):
            assert request.headers["X-Jwt-Token"] == "service-token"
        else:
            assert request.headers["X-Jwt-Token"] == "personal-token"
        return await handler(request)

    return httpx.MockTransport(route)


async def test_openapi_first_native_v2_preserved_and_metrics_union(tmp_path):
    requests = []
    payload = trace_data()

    async def handler(request):
        assert request.url.path == "/openapi/v1/trace/query"
        assert request.headers["X-Flux-Trace-Contract"] == "2"
        assert json.loads(request.content)["include_raw_spans"] is True
        return ok(payload)

    async with httpx.AsyncClient(transport=transport(handler, requests)) as http:
        resources = ResourceService(
            OpenAPIClient(config(tmp_path), http), config(tmp_path)
        )
        data = await resources.trace_and_metrics(
            TraceQuery(task_id="111", include_raw_spans=True)
        )
    assert data["trace"]["data"] == payload
    assert data["trace"]["spans"] == payload["raw_spans"]
    assert data["trace"]["complete"] is True
    assert data["metrics"]["source"] == "flux_insight_openapi"
    values = data["metrics"]["values"]
    assert values["total_tokens"] == 120
    assert values["clean_total_token"] == 90
    assert values["end_to_end_latency_ms"] == 3000
    assert values["total_latency_ms"] == 2500
    assert values["interaction_latency_ms"] == 500
    assert values["model_call_count"] == 2
    assert len(requests) == 2  # IAM + a single shared trace query.


@pytest.mark.parametrize("reason", ["failure", "empty", "incomplete"])
async def test_fornax_fallback_only_when_openapi_missing(tmp_path, reason):
    requests = []

    async def handler(request):
        if request.url.path == "/openapi/v1/trace/query":
            if reason == "failure":
                return httpx.Response(200, json={"code": 3004, "data": {}})
            return ok({} if reason == "empty" else trace_data(complete=False))
        return ok({"spans": [fornax_span()], "has_more": False})

    async with httpx.AsyncClient(transport=transport(handler, requests)) as http:
        resources = ResourceService(
            OpenAPIClient(config(tmp_path), http), config(tmp_path)
        )
        data = await resources.trace_and_metrics(TraceQuery(task_id="111"))
    assert data["trace"]["source"] == (
        "flux_insight_openapi" if reason == "incomplete" else "fornax"
    )
    if reason != "incomplete":
        assert data["trace"]["spans"][0]["input"] == '{"original": true}'
    assert data["trace"]["complete"] is (reason != "incomplete")
    assert "/openapi/v1/trace/query" in [req.url.path for req in requests]
    assert requests[-1].url.path == (
        "/openapi/v1/trace/query"
        if reason == "incomplete"
        else "/open-api/observability/spans/search"
    )
    assert data["metrics"]["source"] == (
        "flux_insight_openapi" if reason == "incomplete" else "fornax"
    )


async def test_failed_fornax_does_not_discard_partial_openapi(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return ok(trace_data(complete=False))
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=transport(handler)) as http:
        resources = ResourceService(
            OpenAPIClient(config(tmp_path), http), config(tmp_path)
        )
        data = await resources.trace_and_metrics(TraceQuery(task_id="111"))
    assert data["trace"]["source"] == "flux_insight_openapi"
    assert data["trace"]["complete"] is False
    assert "fornax" not in data["errors"]


async def test_fornax_missing_cursor_preserves_partial_spans(tmp_path):
    async def handler(request):
        return ok({"spans": [fornax_span()], "has_more": True})

    async with httpx.AsyncClient(transport=transport(handler)) as http:
        resources = ResourceService(
            OpenAPIClient(config(tmp_path), http), config(tmp_path)
        )
        data = await resources.trace_and_metrics(
            TraceQuery(task_id="111", source="fornax")
        )
    assert data["trace"]["complete"] is False
    assert len(data["trace"]["spans"]) == 1
    assert data["fornax"]["error"]["code"] == "FORNAX_PAGINATION_INCOMPLETE"


@pytest.mark.parametrize("content", [b"", b"\x00\xff\x01", b'{"hello": "world"}'])
def test_artifact_bytes_and_checksum_preserved(tmp_path, content):
    async def handler(request):
        assert request.url.path == "/openapi/v1/task/artifacts/raw"
        assert json.loads(request.content) == {
            "task_id": "111",
            "path": "artifacts/spec.md",
        }
        return httpx.Response(
            200,
            content=content,
            headers={
                "Content-Type": "application/json",
                "X-Artifact-Path": "artifacts/spec.md",
                "X-Artifact-SHA256": hashlib.sha256(content).hexdigest(),
                "X-Artifact-Size": str(len(content)),
            },
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/artifacts/raw",
            json={"task_id": "111", "path": "artifacts/spec.md"},
        )
    assert response.status_code == 200
    assert response.content == content
    assert "attachment" in response.headers["Content-Disposition"]


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret",
        "artifacts/../secret",
        "artifacts//a",
        "artifacts/./a",
        "a\\b",
        "a\nb",
    ],
)
def test_artifact_path_rejected_before_network(tmp_path, path):
    async def unexpected(request):
        pytest.fail("Invalid path must not request IAM or Flux")

    with TestClient(
        create_app(config(tmp_path), transport=transport(unexpected))
    ) as client:
        response = client.post(
            "/v1/task-data/artifacts/raw", json={"task_id": "111", "path": path}
        )
    assert response.status_code == 422


@pytest.mark.parametrize("kind", ["error", "checksum", "identity"])
def test_artifact_errors_are_not_returned_as_files(tmp_path, kind):
    async def handler(request):
        if kind == "error":
            return httpx.Response(
                200, json={"code": 2002, "msg": "secret upstream body", "data": {}}
            )
        headers = (
            {"X-Artifact-SHA256": "bad"}
            if kind == "checksum"
            else {"X-Artifact-Task-ID": "222"}
        )
        return httpx.Response(200, content=b"data", headers=headers)

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/artifacts/raw",
            json={"task_id": "111", "path": "artifacts/a"},
        )
    assert response.status_code == 502
    assert "secret upstream body" not in response.text
    assert "Content-Disposition" not in response.headers


def test_download_byte_limit_and_gzip_decoding(tmp_path):
    async def handler(request):
        return httpx.Response(
            200, content=gzip.compress(b"x" * 100), headers={"Content-Encoding": "gzip"}
        )

    with TestClient(
        create_app(
            config(tmp_path, max_download_bytes=50), transport=transport(handler)
        )
    ) as client:
        response = client.post(
            "/v1/task-data/artifacts/raw",
            json={"task_id": "111", "path": "artifacts/a"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOWNLOAD_LIMIT_EXCEEDED"


async def test_export_isolated_failures_binary_files_and_session_pinning(tmp_path):
    requests = []
    content = b"\x00\xff"

    async def handler(request):
        body = json.loads(request.content)
        match request.url.path:
            case "/openapi/v1/task":
                if body.get("messages_only") or body.get("include_trace"):
                    return ok(
                        {
                            "trace_detail_status": "available",
                            "trace_detail": {
                                "data": {
                                    "messages": [
                                        {"role": "user", "content": "question"},
                                        {"role": "assistant", "content": "answer"},
                                    ]
                                },
                                "meta": {"complete": True},
                            },
                        }
                    )
                return ok(
                    {
                        "items": [
                            {
                                "task_id": "111",
                                "id_kind": "task",
                                "task": {"worker_session_id": "999"},
                            }
                        ],
                        "page_info": {"has_more": False},
                    }
                )
            case "/openapi/v1/trace/query":
                return ok(trace_data())
            case "/openapi/v1/task/changed/artifacts":
                return ok(
                    {
                        "task_id": "111",
                        "worker_session_id": "999",
                        "file_count": 2,
                        "files": [
                            {
                                "path": "artifacts/a.bin",
                                "size": 2,
                                "sha256": hashlib.sha256(content).hexdigest(),
                            },
                            {"path": "artifacts/missing.md"},
                        ],
                    }
                )
            case "/openapi/v1/task/artifacts/raw":
                assert body["worker_session_id"] == "999"
                assert "task_id" not in body
                if body["path"].endswith("missing.md"):
                    return httpx.Response(
                        200, json={"code": 1001, "msg": "not found", "data": {}}
                    )
                return httpx.Response(200, content=content)
            case "/openapi/v1/task/artifacts/versions":
                assert body["worker_session_id"] == "999"
                return httpx.Response(200, json={"code": 1001, "data": {}})
            case _:
                pytest.fail(f"Unexpected request {request.url.path}")

    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=transport(handler, requests)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(
            ExportQuery(
                task_ids=["111", "111"],
                components=["task", "trace", "metrics", "artifacts", "versions"],
            )
        )
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["status"] == "partial"
    assert final["completed_count"] == final["task_count"] == 1
    assert final["tasks"]["111"]["components"]["artifacts"]["downloaded_count"] == 1
    assert final["archive_ready"] is True
    reader = TaskDataReader(tmp_path / job["job_id"] / "tasks/111")
    assert reader.validate()["valid"]
    assert reader.read_dataset("artifacts")["status"] == "partial"
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        assert archive.read("tasks/111/artifacts/files/artifacts/a.bin") == content
        assert "tasks/111/trace/openapi.json" in archive.namelist()
        assert "tasks/111/metrics.json" in archive.namelist()
        assert (
            "tasks/111/artifacts/files/artifacts/missing.md" not in archive.namelist()
        )
    assert sum(req.url.path.endswith("/trace/query") for req in requests) == 1


@pytest.mark.parametrize("ending", ["complete", "cancel", "timeout"])
async def test_export_file_progress_while_download_pending(tmp_path, ending):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        if request.url.path.endswith("/changed/artifacts"):
            return ok(
                {
                    "task_id": "111",
                    "file_count": 3,
                    "files": [{"path": f"artifacts/{name}"} for name in "abc"],
                }
            )
        path = json.loads(request.content)["path"]
        if path.endswith("b"):
            return httpx.Response(403)
        if path.endswith("c"):
            entered.set()
            await release.wait()
        return httpx.Response(200, content=b"file")

    cfg = config(tmp_path, export_task_timeout_seconds=3 if ending == "timeout" else 30)
    async with httpx.AsyncClient(transport=transport(handler)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["artifacts"]))
        worker = manager.running[job["job_id"]]
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            live = await manager.get_async(job["job_id"])
            assert live["completed_count"] == 0
            assert live["tasks"]["111"]["progress"] == {
                "stage": "artifacts",
                "file_count": 3,
                "completed_count": 2,
                "downloaded_count": 1,
                "failed_count": 1,
            }
            if ending == "cancel":
                manager.cancel(job["job_id"])
            elif ending == "complete":
                release.set()
            await asyncio.wait_for(asyncio.shield(worker), timeout=5)
        finally:
            release.set()
            await worker
        final = manager.get(job["job_id"])
    task = final["tasks"]["111"]
    assert "progress" not in task
    assert final["archive_ready"] is True
    if ending == "complete":
        assert task["components"]["artifacts"]["downloaded_count"] == 2
        assert final["status"] == "partial"
    elif ending == "cancel":
        assert final["status"] == "cancelled"
    else:
        assert task["components"]["timeout"]["error"]["code"] == "QUERY_TIMEOUT"


async def test_export_cancel_and_busy_limit(tmp_path):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(10)
        return ok({})

    cfg = config(tmp_path, max_export_jobs=1)
    async with httpx.AsyncClient(transport=transport(handler)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["task"]))
        await entered.wait()
        assert (
            TaskDataReader(tmp_path / job["job_id"] / "tasks/111").manifest.finalized
            is False
        )
        with pytest.raises(Exception, match="Too many active export jobs"):
            manager.create(ExportQuery(task_ids=["222"]))
        manager.cancel(job["job_id"])
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["status"] == "cancelled"
    assert final["archive_ready"] is True
    assert (
        TaskDataReader(tmp_path / job["job_id"] / "tasks/111").manifest.finalized
        is True
    )


def test_export_http_auth_and_disk_recovery(tmp_path):
    async def handler(request):
        return ok(
            {
                "items": [{"task_id": "111", "id_kind": "task"}],
                "page_info": {"has_more": False},
            }
        )

    cfg = config(tmp_path, api_key=SecretStr("caller-key"))
    headers = {"Authorization": "Bearer caller-key"}
    with TestClient(create_app(cfg, transport=transport(handler))) as client:
        assert client.post("/v1/exports", json={"task_ids": ["111"]}).status_code == 401
        response = client.post(
            "/v1/exports",
            json={"task_ids": ["111"], "components": ["task"]},
            headers=headers,
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(100):
            job = client.get(f"/v1/exports/{job_id}", headers=headers).json()
            if job["archive_ready"]:
                break
            time.sleep(0.01)
        assert job["archive_ready"]
        assert client.get(f"/v1/exports/{job_id}/download").status_code == 401
        response = client.get(f"/v1/exports/{job_id}/download", headers=headers)
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert "tasks/111/task.json" in archive.namelist()
    with TestClient(create_app(cfg)) as client:
        assert (
            client.get(f"/v1/exports/{job_id}", headers=headers).json()["status"]
            == "completed"
        )
        assert client.get("/v1/exports/not-a-job", headers=headers).status_code == 404


def test_datetime_offsets_are_respected():
    assert _iso_to_ms("2026-11-01T08:00:00+08:00") == _iso_to_ms("2026-11-01T00:00:00Z")
    assert _iso_to_ms("2026-02-30T00:00:00Z") == 0


async def test_queued_cancellation_finalizes_archive(tmp_path):
    async def unexpected(request):
        pytest.fail("Queued cancellation must not make network calls")

    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=transport(unexpected)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["task"]))
        task = manager.running[job["job_id"]]
        manager.cancel(job["job_id"])
        await task
        final = manager.get(job["job_id"])
    assert final["status"] == "cancelled"
    assert final["archive_ready"] is True
    assert final["completed_count"] == 0
    reader = TaskDataReader(tmp_path / job["job_id"] / "tasks/111")
    assert reader.manifest.finalized is True
    assert reader.manifest.task_status == "cancelled"
    assert reader.read_dataset("task")["status"] == "unavailable"
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        assert json.loads(archive.read("job.json"))["archive_ready"] is True


async def test_manifest_byte_limit_never_publishes_ready_archive(tmp_path):
    async def unexpected(request):
        pytest.fail("Manifest byte limit should stop before network access")

    cfg = config(tmp_path, max_export_bytes=100)
    async with httpx.AsyncClient(transport=transport(unexpected)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["task"]))
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["status"] == "failed"
    assert final["archive_ready"] is False
    assert not (tmp_path / job["job_id"] / "export.zip").exists()


async def test_component_byte_limit_can_still_publish_partial_archive(tmp_path):
    async def handler(request):
        return ok(
            {
                "items": [
                    {"task_id": "111", "id_kind": "task", "content": "x" * 50000}
                ],
                "page_info": {"has_more": False},
            }
        )

    cfg = config(tmp_path, max_export_bytes=10000)
    async with httpx.AsyncClient(transport=transport(handler)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["task"]))
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["status"] == "partial"
    assert final["archive_ready"] is True
    assert (
        final["tasks"]["111"]["components"]["task"]["error"]["code"]
        == "EXPORT_LIMIT_EXCEEDED"
    )
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        manifest = json.loads(archive.read("tasks/111/data-manifest.json"))
        assert manifest["finalized"] is True
        assert manifest["datasets"]["task"]["status"] == "unavailable"
        assert "tasks/111/task.json" not in archive.namelist()


async def test_manifest_replacement_counts_current_size_and_flags_invalid_data(
    tmp_path,
):
    cfg = config(tmp_path)
    async with httpx.AsyncClient() as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = {
            "job_id": "a" * 32,
            "bytes_written": 0,
            "tasks": {"111": {"status": "ok", "components": {}}},
        }
        query = ExportQuery(task_ids=["111"], components=["task"])
        manager.save_task_manifest(job, "111", query, finalized=False)
        before = job["bytes_written"]
        manager.save_task_manifest(job, "111", query, finalized=False)
        assert job["bytes_written"] == before
        manager.write(job, "tasks/111/task.json", b"[]")
        job["tasks"]["111"]["components"]["task"] = {
            "status": "ok",
            "source": "flux_insight_openapi",
            "file": "tasks/111/task.json",
        }
        manager.save_task_manifest(job, "111", query, finalized=True)
    assert job["tasks"]["111"]["status"] == "partial"
    assert job["tasks"]["111"]["contract_errors"][0]["code"] == "INVALID_PAYLOAD"
    reader = TaskDataReader(tmp_path / job["job_id"] / "tasks/111")
    assert reader.manifest.task_status == "partial"
    assert reader.validate()["valid"] is False


async def test_export_conversation_pagination_and_gui_screenshots(tmp_path):
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/openapi/v1/task":
            assert body.get("include_trace") or body.get("messages_only")
            return httpx.Response(200, json={"code": 1005, "data": {}})
        if request.url.path.endswith("/task/trace"):
            if body["action"] == "inventory":
                return ok(
                    {
                        "batches": [{"ticket": "batch-1"}],
                        "sessions": [{"ticket": "session-1"}],
                    }
                )
            if body["action"] == "messages":
                assert body["ticket"] == "session-1"
                return ok(
                    {
                        "data": {
                            "messages": [{"role": "assistant", "content": "message"}]
                        },
                        "meta": {"has_more": False, "complete": True},
                    }
                )
            assert body["ticket"] == "batch-1"
            return ok(
                {
                    "data": {"content": "chunk"},
                    "meta": {
                        "has_more": "cursor" not in body,
                        "complete": True,
                        "next_cursor": "next" if "cursor" not in body else None,
                    },
                }
            )
        if request.url.path.endswith("/case-result"):
            return ok(
                {
                    "items": [
                        {
                            "case_result": {
                                "screenshot": "https://storage.invalid/TC-001.png"
                            }
                        }
                    ]
                }
            )
        if request.url.path.endswith("/screenshots"):
            assert body["path"] == "TC-001.png"
            return httpx.Response(
                200, content=b"fake-png", headers={"Content-Type": "image/png"}
            )
        pytest.fail(f"Unexpected request {request.url.path}")

    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=transport(handler, requests)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(
            ExportQuery(task_ids=["111"], components=["gui", "conversation"])
        )
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["status"] == "completed"
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        assert archive.read("tasks/111/gui/screenshots/0.png") == b"fake-png"
        assert (
            len([name for name in archive.namelist() if "/conversation/" in name]) == 4
        )
    assert all(req.url.host != "storage.invalid" for req in requests)


@pytest.mark.parametrize("missing_task", [False, True])
async def test_export_saves_all_task_details_before_other_components(
    tmp_path, missing_task
):
    events = []
    ids = ["111", "222", "333"]
    job = None

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/openapi/v1/task":
            task_id = body["task_id_list"][0]
            events.append(("task", task_id))
            return ok(
                {
                    "items": []
                    if missing_task and task_id == "222"
                    else [
                        {
                            "task_id": task_id,
                            "id_kind": "task",
                            "task": {"created_by": "alice"},
                        }
                    ],
                    "page_info": {"has_more": False},
                }
            )
        assert request.url.path == "/openapi/v1/task/changed/artifacts"
        assert job["completed_count"] < len(ids)
        for task_id in ids:
            record = job["tasks"][task_id]["components"]["task"]
            if missing_task and task_id == "222":
                assert record["status"] == "error"
            else:
                assert record["status"] == "ok"
                assert (tmp_path / job["job_id"] / record["file"]).is_file()
        events.append(("artifacts", body["task_id"]))
        return ok({"task_id": body["task_id"], "file_count": 0, "files": []})

    cfg = config(tmp_path, max_concurrent_queries=1)
    async with httpx.AsyncClient(transport=transport(handler)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(1)
        )
        job = manager.create(
            ExportQuery(task_ids=ids, components=["artifacts", "task"])
        )
        await manager.running[job["job_id"]]
    assert events == [("task", task_id) for task_id in ids] + [
        ("artifacts", task_id) for task_id in ids
    ]
    assert job["completed_count"] == len(ids)
    assert job["status"] == ("partial" if missing_task else "completed")


async def test_filter_export_scans_all_ids_then_fetches_details(tmp_path):
    requests = []

    async def handler(request):
        assert request.url.path == "/openapi/v1/task"
        body = json.loads(request.content)
        if "filters" in body:
            return ok(
                {
                    "items": [
                        {"task_id": "111", "id_kind": "task"},
                        {"task_id": "999", "id_kind": "session"},
                    ],
                    "page_info": {"has_more": False},
                }
            )
        assert body["task_id_list"] == ["111"]
        return ok(
            {
                "items": [{"task_id": "111", "id_kind": "task"}],
                "page_info": {"has_more": False},
            }
        )

    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=transport(handler, requests)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(
            ExportQuery(
                filters={
                    "filter_fields": [
                        {
                            "field_name": "created_by",
                            "query_type": "in",
                            "values": ["alice"],
                        }
                    ]
                },
                components=["task"],
            )
        )
        await manager.running[job["job_id"]]
        final = manager.get(job["job_id"])
    assert final["task_count"] == 1
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        assert json.loads(archive.read("task_ids.json")) == ["111"]
        assert json.loads(archive.read("selection.json"))["skipped_session_count"] == 1


async def test_desktop_personal_refresh_is_separate_from_service_auth(
    tmp_path, monkeypatch
):
    from flux_task_service.resources import FornaxClient

    monkeypatch.setattr(
        FornaxClient, "chrome_cookies", staticmethod(lambda: "CAS_SESSION=test-sso")
    )
    sent = []

    async def handler(request):
        sent.append(request)
        if request.url.path == "/auth/api/v1/jwt":
            if "Cookie" in request.headers:
                assert "Authorization" not in request.headers
                return httpx.Response(
                    200, headers={"X-Jwt-Token": "fresh-personal-token"}
                )
            assert request.headers["Authorization"] == "Bearer service-secret"
            return httpx.Response(200, headers={"X-Jwt-Token": "service-token"})
        if request.url.path.endswith("/trace/query"):
            assert request.headers["X-Jwt-Token"] == "service-token"
            return httpx.Response(200, json={"code": 3004, "data": {}})
        if request.headers["X-Jwt-Token"] == "personal-token":
            return httpx.Response(401)
        assert request.headers["X-Jwt-Token"] == "fresh-personal-token"
        return ok({"spans": [fornax_span()], "has_more": False})

    cfg = config(tmp_path, fornax_refresh_from_chrome=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resources = ResourceService(OpenAPIClient(cfg, http), cfg)
        data = await resources.trace_and_metrics(TraceQuery(task_id="111"))
        # Refresh does not alter the service account token cache.
        assert await resources.api.auth.get_token() == "service-token"
    assert data["trace"]["source"] == "fornax"
    assert sum("Cookie" in request.headers for request in sent) == 1


async def test_export_keeps_artifacts_named_zip_and_tmp(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/changed/artifacts"):
            return ok(
                {
                    "task_id": "111",
                    "file_count": 3,
                    "files": [
                        {"path": path}
                        for path in [
                            "artifacts/export.zip",
                            "artifacts/job.json",
                            "artifacts/log.tmp",
                        ]
                    ],
                }
            )
        return httpx.Response(200, content=b"file")

    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=transport(handler)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        job = manager.create(ExportQuery(task_ids=["111"], components=["artifacts"]))
        await manager.running[job["job_id"]]
    with zipfile.ZipFile(tmp_path / job["job_id"] / "export.zip") as archive:
        for name in ["export.zip", "job.json", "log.tmp"]:
            assert (
                archive.read(f"tasks/111/artifacts/files/artifacts/{name}") == b"file"
            )


def test_personal_jwt_expiry_claim(tmp_path):
    import base64

    from flux_task_service.resources import FornaxClient

    def token(expiry):
        payload = (
            base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode())
            .decode()
            .rstrip("=")
        )
        return f"header.{payload}.signature"

    assert FornaxClient.expired(token(time.time() - 100))
    assert not FornaxClient.expired(token(time.time() + 3600))


def test_default_read_model_does_not_request_raw_or_fornax(tmp_path):
    payload = trace_data()
    payload.pop("raw_spans")

    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            assert json.loads(request.content)["include_raw_spans"] is False
            return ok(payload)
        assert request.url.path == "/openapi/v1/task"
        return ok(
            {
                "trace_detail": {
                    "data": {"messages": [{"content": "complete conversation"}]},
                    "meta": {"complete": True},
                }
            }
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post("/v1/task-data/trace", json={"task_id": "111"})
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "flux_insight_openapi"
    assert data["complete"] is True
    assert data["spans"] == []
    assert data["data"] == payload
    assert data["raw_spans_complete"] is None
    assert data["raw_spans_status"] == "not_requested"
    assert data["warnings"] == []
    assert "raw_recovery" not in data


def test_metadata_projection_is_not_a_complete_conversation(tmp_path):
    payload = trace_data()
    payload.pop("raw_spans")
    payload["meta"]["projection"] = {
        "mode": "metadata_only",
        "source_complete": True,
        "omitted": ["content", "step_payloads", "raw_spans"],
    }

    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return ok(payload)
        if request.url.path == "/openapi/v1/task":
            body = json.loads(request.content)
            assert body.get("include_trace") or body.get("messages_only")
            return httpx.Response(200, json={"code": 2002, "data": {}})
        pytest.fail("OpenAPI-only mode must not fall back to Fornax or Web APIs")

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/trace", json={"task_id": "111", "source": "openapi"}
        )
    assert response.status_code == 200
    data = response.json()
    assert data["complete"] is False
    assert data["execution_complete"] is True
    assert data["conversation_complete"] is False
    assert data["conversation"]["content_available"] is False
    assert data["conversation"]["errors"][0]["code"] == "CONVERSATION_FORBIDDEN"
    assert any("正文" in warning for warning in data["warnings"])


def test_trace_includes_full_conversation_when_authorized(tmp_path):
    payload = trace_data()
    messages = [
        {"role": "user", "content": "完整问题"},
        {"role": "assistant", "content": "完整回答"},
    ]

    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return ok(payload)
        assert request.url.path == "/openapi/v1/task"
        assert json.loads(request.content).get("include_trace") or json.loads(
            request.content
        ).get("messages_only")
        return ok(
            {
                "trace_detail_status": "available",
                "trace_detail": {
                    "data": {"messages": messages},
                    "meta": {"complete": True},
                },
            }
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/trace", json={"task_id": "111", "source": "openapi"}
        )
    data = response.json()
    assert data["complete"] is True
    assert data["conversation_complete"] is True
    assert (
        data["conversation"]["documents"][0]["data"]["trace_detail"]["data"]["messages"]
        == messages
    )


def test_conversation_endpoint_success_without_text_is_not_complete(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return ok(trace_data())
        if request.url.path == "/openapi/v1/task":
            return ok(
                {
                    "trace_detail_status": "available",
                    "trace_detail": {
                        "data": {"sessions": [{"id": "s1"}]},
                        "meta": {
                            "complete": True,
                            "projection": {
                                "mode": "metadata_only",
                                "omitted": ["content"],
                            },
                        },
                    },
                }
            )
        pytest.fail("Hidden content must not be silently treated as full conversation")

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        data = client.post(
            "/v1/task-data/trace", json={"task_id": "111", "source": "openapi"}
        ).json()
    assert data["complete"] is False
    assert data["conversation"]["errors"][0]["code"] == "CONVERSATION_CONTENT_HIDDEN"


@pytest.mark.parametrize("limit_error", ["byte_limit", "upstream_limit"])
def test_large_raw_trace_falls_back_to_openapi_before_fornax(tmp_path, limit_error):
    raw_flags = []
    payload = trace_data()
    payload.pop("raw_spans")

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/trace/query"):
            raw_flags.append(body["include_raw_spans"])
            if body["include_raw_spans"]:
                if limit_error == "upstream_limit":
                    return httpx.Response(200, json={"code": 1005, "data": {}})
                return ok({"raw_spans": ["x" * 8192]})
            return ok(payload)
        assert request.url.path == "/openapi/v1/task", (
            "Fornax must not replace available OpenAPI data"
        )
        assert body["include_trace"] is True
        return ok(
            {
                "trace_detail_status": "available",
                "trace_detail": {
                    "data": {
                        "messages": [{"role": "assistant", "content": "full answer"}]
                    },
                    "meta": {"complete": True, "completenessScope": "trace-view"},
                },
            }
        )

    with TestClient(
        create_app(
            config(tmp_path, max_download_bytes=4096), transport=transport(handler)
        )
    ) as client:
        response = client.post(
            "/v1/task-data/trace",
            json={"task_id": "111", "include_raw_spans": True},
        )
    assert response.status_code == 200
    data = response.json()
    assert raw_flags == [True, False]
    assert data["source"] == "flux_insight_openapi"
    assert data["raw_spans_complete"] is False
    assert data["conversation_complete"] is True
    assert "openapi_raw" in data["fallback_errors"]


@pytest.mark.parametrize(
    "scenario",
    [
        "complete",
        "missing",
        "session_limit",
        "timeout",
        "byte_limit",
        "transient",
        "single_session",
    ],
)
def test_session_raw_recovery_http_contract_is_bounded_and_task_scoped(
    tmp_path, scenario
):
    calls = []
    payload = trace_data()
    payload.pop("raw_spans")
    payload["meta"]["counts"]["spans"] = 2
    payload["sessions"] = [
        {"id": "1001", "relation": {"source_span_ids": ["s1"]}, "turns": []},
        {"id": "1002", "relation": {"source_span_ids": ["s2"]}, "turns": []},
    ]
    if scenario == "single_session":
        payload["sessions"] = payload["sessions"][:1]
    first = {
        "trace_id": "t1",
        "span_id": "s1",
        "payload": {"task_id": "111", "input": "original"},
    }
    # A conflicting owner must be excluded even when its ID appears in evidence.
    foreign = {
        "trace_id": "t1",
        "span_id": "s2",
        "payload": {"task_id": "222"},
    }
    # Untagged originals can only be selected through task-scoped evidence.
    second = {"trace_id": "t1", "span_id": "s2", "payload": {"input": "original"}}
    unrelated = {"trace_id": "t1", "span_id": "other", "payload": {}}

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path == "/openapi/v1/task":
            return ok(
                {
                    "trace_detail": {
                        "data": {"messages": [{"content": "complete conversation"}]},
                        "meta": {"complete": True},
                    }
                }
            )
        assert request.url.path == "/openapi/v1/trace/query", (
            "Session recovery must preserve OpenAPI and never call Fornax"
        )
        if body.get("task_id"):
            if body["include_raw_spans"]:
                return httpx.Response(200, json={"code": 1005, "data": {}})
            return ok(payload)
        assert "task_id" not in body
        assert body["include_raw_spans"] is True
        assert request.headers["X-Flux-Trace-Contract"] == "2"
        sid = body["worker_session_id"]
        calls.append(sid)
        if scenario == "timeout":
            await asyncio.sleep(0.2)
        if scenario == "byte_limit":
            return ok({"raw_spans": [first], "padding": "x" * 8192})
        if scenario == "transient":
            return httpx.Response(503, json={"code": 3001, "data": {}})
        if sid == "1002" and scenario == "missing":
            return httpx.Response(200, json={"code": 3004, "data": {}})
        return ok(
            {
                "meta": {"complete": True},
                "raw_spans": [first, foreign, unrelated]
                if sid == "1001"
                else [first, second],
            }
        )

    overrides = {"max_retries": 3}
    if scenario == "session_limit":
        overrides["raw_recovery_max_sessions"] = 1
    if scenario == "timeout":
        overrides["raw_recovery_timeout_seconds"] = 0.15
    if scenario == "byte_limit":
        overrides["raw_recovery_max_bytes"] = 1024
    with TestClient(
        create_app(config(tmp_path, **overrides), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/trace",
            json={
                "task_id": "111",
                "source": "openapi",
                "include_raw_spans": True,
                "recover_raw_spans": True,
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "flux_insight_openapi"
    assert data["data"] == payload
    assert data["conversation_complete"] is True
    assert data["raw_spans_complete"] is (scenario == "complete")
    recovery = data["raw_recovery"]
    assert recovery["complete"] is (scenario == "complete")
    assert all(
        span.get("payload", {}).get("task_id") != "222" for span in data["spans"]
    )
    if scenario == "complete":
        assert data["spans"] == [first, second]
        assert recovery["status"] == "complete"
        assert data["warnings"] == []
    else:
        assert data["warnings"]
    assert calls == (
        []
        if scenario == "single_session"
        else (
            ["1001"]
            if scenario in {"session_limit", "timeout", "byte_limit"}
            else ["1001", "1002"]
        )
    )
    if scenario == "single_session":
        assert recovery["status"] == "not_attempted"
        assert recovery["errors"][0]["code"] == "RAW_RECOVERY_NOT_SPLITTABLE"
    if scenario == "transient":
        assert all(
            session["error"]["code"] == "UPSTREAM_RETRIES_EXHAUSTED"
            for session in recovery["sessions"]
        )
    if scenario == "missing":
        assert recovery["sessions"][1]["error"]["upstream_code"] == 3004


@pytest.mark.parametrize(
    "mode,flag,scope",
    [
        ("full", "include_trace", "trace-view"),
        ("messages", "messages_only", "selected-session-message-text"),
    ],
)
def test_conversation_mode_and_coverage_remain_explicit(tmp_path, mode, flag, scope):
    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return ok(trace_data())
        body = json.loads(request.content)
        assert body[flag] is True
        assert ("messages_only" if mode == "full" else "include_trace") not in body
        return ok(
            {
                "trace_detail_status": "available",
                "trace_detail": {
                    "data": {"messages": [{"role": "assistant", "content": "answer"}]},
                    "meta": {
                        "complete": True,
                        "completenessScope": scope,
                        "scope": {"taskId": "111"},
                        "omitted": ["reasoning"] if mode == "messages" else [],
                        "resourcesNotEmbedded": ["external images"],
                        "coverage": [{"source": "messages", "status": "complete"}],
                    },
                },
            }
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        data = client.post(
            "/v1/task-data/trace",
            json={"task_id": "111", "source": "openapi", "conversation_mode": mode},
        ).json()
    conversation = data["conversation"]
    assert conversation["mode"] == mode
    assert conversation["completeness_scope"] == scope
    assert conversation["resources_not_embedded"] == ["external images"]
    assert conversation["scope"] == {"taskId": "111"}
    assert data["completeness_scope"]["conversation"] == scope


def test_metrics_only_does_not_request_large_raw_spans(tmp_path):
    async def handler(request):
        assert request.url.path.endswith("/trace/query")
        assert json.loads(request.content)["include_raw_spans"] is False
        return ok(trace_data())

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post("/v1/task-data/metrics", json={"task_id": "111"})
    assert response.status_code == 200
    assert response.json()["source"] == "flux_insight_openapi"
    assert response.json()["values"]["total_tokens"] == 120


@pytest.mark.parametrize(
    "extra",
    [
        {"conversation_mode": "messages"},
        {"include_raw_spans": True},
        {"recover_raw_spans": True},
    ],
)
def test_metrics_rejects_trace_only_fields(tmp_path, extra):
    with TestClient(create_app(config(tmp_path))) as client:
        response = client.post(
            "/v1/task-data/metrics", json={"task_id": "111", **extra}
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"


@pytest.mark.parametrize("include_raw", [False, True])
def test_trace_export_keeps_payload_and_conversation_once(tmp_path, include_raw):
    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/trace/query"):
            assert body["include_raw_spans"] is include_raw
            payload = trace_data()
            payload["sessions"][0]["content"] = "execution " * 4000
            if include_raw:
                payload["raw_spans"][0]["payload"]["input"] = "raw " * 4000
            else:
                payload.pop("raw_spans")
            return ok(payload)
        assert request.url.path == "/openapi/v1/task"
        return ok(
            {
                "trace_detail": {
                    "data": {"messages": [{"content": "conversation " * 4000}]},
                    "meta": {"complete": True, "completenessScope": "trace-view"},
                }
            }
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/exports",
            json={
                "task_ids": ["111"],
                "components": ["trace"],
                "include_raw_spans": include_raw,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/v1/exports/{job_id}").json()
            if job["archive_ready"]:
                break
            time.sleep(0.01)
        assert job["status"] == "completed"
        archive_bytes = client.get(f"/v1/exports/{job_id}/download").content
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        trace = json.loads(archive.read("tasks/111/trace.json"))
        manifest = json.loads(archive.read("tasks/111/data-manifest.json"))
        assert manifest["schema_version"] == "1.0"
        assert manifest["finalized"] is True
        assert manifest["datasets"]["execution"]["status"] == "available"
        assert manifest["datasets"]["conversation"]["status"] == "available"
        assert manifest["datasets"]["raw_spans"]["status"] == (
            "available" if include_raw else "not_requested"
        )
        assert "data" not in trace
        assert "documents" not in trace["conversation"]
        assert trace["conversation"]["file"] == "tasks/111/conversation.json"
        assert len(archive.read("tasks/111/trace.json")) < 10000
        assert "execution " in archive.read(trace["data_file"]).decode()
        conversation = json.loads(archive.read(trace["conversation"]["files"][0]))
        assert conversation["trace_detail"]["data"]["messages"][0][
            "content"
        ].startswith("conversation ")
        assert trace["raw_spans_complete"] is (True if include_raw else None)
        if include_raw:
            assert "spans" not in trace
            assert trace["spans_file"] == trace["data_file"]
            assert trace["spans_pointer"] == "/raw_spans"
            assert (
                "raw "
                in json.loads(archive.read(trace["spans_file"]))["raw_spans"][0][
                    "payload"
                ]["input"]
            )


def test_fornax_rejected_jwt_is_shared_until_file_changes(tmp_path):
    jwt_file = tmp_path / "personal.jwt"
    jwt_file.write_text("bad-token")
    calls = []

    async def handler(request):
        assert request.url.path == "/open-api/observability/spans/search"
        token = request.headers["X-Jwt-Token"]
        calls.append(token)
        if token == "bad-token":
            return httpx.Response(401)
        return ok({"spans": [fornax_span()], "has_more": False})

    cfg = config(tmp_path, fornax_jwt=None, fornax_jwt_file=jwt_file)
    with TestClient(create_app(cfg, transport=httpx.MockTransport(handler))) as client:
        for _ in range(2):
            response = client.post(
                "/v1/task-data/trace", json={"task_id": "111", "source": "fornax"}
            )
            assert response.status_code == 502
        jwt_file.write_text("fresh-token")
        response = client.post(
            "/v1/task-data/trace", json={"task_id": "111", "source": "fornax"}
        )
        assert response.status_code == 200
    assert calls == ["bad-token", "fresh-token"]


def test_experimental_recovery_requires_explicit_raw_request(tmp_path):
    with TestClient(create_app(config(tmp_path))) as client:
        for path, body in [
            ("/v1/task-data/trace", {"task_id": "111"}),
            ("/v1/exports", {"task_ids": ["111"]}),
        ]:
            response = client.post(path, json={**body, "recover_raw_spans": True})
            assert response.status_code == 422


@pytest.mark.parametrize(
    "scenario",
    [
        "unchanged",
        "changed",
        "removed",
        "persistent",
        "download_failed",
        "refresh_failed",
        "forbidden",
        "timeout",
        "identity_changed",
        "empty",
        "byte_limit",
    ],
)
def test_artifact_export_refreshes_once_and_bounds_each_file(tmp_path, scenario):
    calls = {"manifest": 0, "a": 0, "b": 0}
    original = b"" if scenario == "empty" else b"old"
    replacement = b"new version" if scenario == "changed" else original
    second = b"second"

    def entry(path, content):
        return {
            "path": path,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/changed/artifacts"):
            calls["manifest"] += 1
            refresh = calls["manifest"] > 1
            if refresh and scenario == "refresh_failed":
                return httpx.Response(503, json={"code": 9000, "data": {}})
            if refresh and scenario == "timeout":
                await asyncio.sleep(0.2)
            files = [
                entry("artifacts/a", replacement if refresh else original),
                entry("artifacts/b", second),
            ]
            if refresh and scenario == "removed":
                files = files[1:]
            return ok(
                {
                    "task_id": "222"
                    if refresh and scenario == "identity_changed"
                    else "111",
                    "worker_session_id": "999",
                    "file_count": len(files),
                    "files": files,
                }
            )
        assert request.url.path == "/openapi/v1/task/artifacts/raw"
        assert body["worker_session_id"] == "999"
        name = "a" if body["path"] == "artifacts/a" else "b"
        calls[name] += 1
        if name == "a" and scenario == "forbidden":
            return httpx.Response(200, json={"code": 2002, "data": {}})
        if name == "a" and scenario == "byte_limit":
            return httpx.Response(200, content=b"x" * 8192)
        failed = (
            name == "a"
            and (calls[name] == 1 or scenario in {"persistent", "download_failed"})
        ) or (
            name == "b"
            and scenario in {"persistent", "refresh_failed"}
            and calls[name] == 1
        )
        if failed:
            return httpx.Response(
                200,
                json={
                    "code": 3001,
                    "msg": "下载 worker 产物失败"
                    if scenario == "download_failed"
                    else "产物内容与 manifest 的 size/sha256 不一致，请重新读取",
                    "data": {},
                },
            )
        return httpx.Response(200, content=replacement if name == "a" else second)

    overrides = {"max_retries": 3}
    if scenario == "timeout":
        overrides["artifact_recovery_timeout_seconds"] = 0.15
    if scenario == "byte_limit":
        overrides["max_download_bytes"] = 4096
    with TestClient(
        create_app(config(tmp_path, **overrides), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/exports",
            json={
                "task_ids": ["111"],
                "components": ["artifacts"],
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(300):
            job = client.get(f"/v1/exports/{job_id}").json()
            if job["archive_ready"]:
                break
            time.sleep(0.01)
        assert job["archive_ready"]
        component = job["tasks"]["111"]["components"]["artifacts"]
        records = json.loads((tmp_path / job_id / component["file"]).read_text())
    a, b = records
    assert calls["manifest"] == (1 if scenario in {"forbidden", "byte_limit"} else 2)
    assert calls["a"] <= 2 and calls["b"] <= 2
    assert b["status"] == ("error" if scenario == "refresh_failed" else "ok")
    if scenario in {"unchanged", "changed", "empty"}:
        assert component["status"] == "ok" and component["recovered_count"] == 1
        assert a["attempts"] == 2 and a["recovered"]
        assert a["verified_against"] == "refreshed"
        assert (tmp_path / job_id / a["file"]).read_bytes() == replacement
        assert a["manifest_changed"] is (scenario == "changed")
        if scenario == "changed":
            assert a["initial_expected"]["sha256"] != a["verified_expected"]["sha256"]
    else:
        assert component["status"] == "partial" and a["status"] == "error"
        expected = {
            "removed": "ARTIFACT_REMOVED",
            "persistent": "ARTIFACT_INTEGRITY_ERROR",
            "download_failed": "ARTIFACT_DOWNLOAD_FAILED",
            "forbidden": "UPSTREAM_FORBIDDEN",
            "timeout": "ARTIFACT_RECOVERY_TIMEOUT",
            "byte_limit": "DOWNLOAD_LIMIT_EXCEEDED",
        }
        if scenario in expected:
            assert a["error"]["code"] == expected[scenario]
        assert not (
            tmp_path / job_id / "tasks/111/artifacts/files/artifacts/a"
        ).exists()
    if scenario in {
        "removed",
        "refresh_failed",
        "forbidden",
        "timeout",
        "identity_changed",
        "byte_limit",
    }:
        assert calls["a"] == 1
    if scenario == "persistent":
        assert calls["b"] == 2 and b["recovered"]
    if scenario in {"refresh_failed", "identity_changed"}:
        assert component["manifest_refresh_error"]
        assert component["refreshed_manifest_file"] is None


@pytest.mark.parametrize(
    "scenario",
    [
        "complete",
        "partial_turn",
        "invalid_duration",
        "zero_duration",
        "missing_start",
        "missing_first",
        "missing_call_start",
        "model_first_zero",
        "session_count_mismatch",
        "total_count_mismatch",
        "source_unknown",
        "subagent",
    ],
)
def test_metrics_http_checks_timing_coverage_and_unknown_latency(tmp_path, scenario):
    payload = trace_data()
    primary = payload["sessions"][0]
    for turn in primary["turns"]:
        turn["timing"]["ttft_ms"] = 0
    first = 0
    known = total = 3
    union = 2500
    complete = True
    counts_consistent = True
    if scenario == "partial_turn":
        primary["turns"][1]["timing"].pop("ended_at")
        known, union, complete = 2, 2000, False
    elif scenario == "invalid_duration":
        primary["turns"][1]["timing"].pop("ended_at")
        primary["turns"][1]["timing"]["duration_ms"] = "bad"
        known, union, complete = 2, 2000, False
    elif scenario == "zero_duration":
        primary["turns"][1]["timing"].pop("ended_at")
        primary["turns"][1]["timing"]["duration_ms"] = 0
        union = 2000
    elif scenario == "missing_start":
        primary["turns"][0]["timing"].pop("started_at")
        known, union, complete, first = 2, 2000, False, None
    elif scenario == "missing_first":
        primary["turns"][0]["model_calls"] = [{"started_at": 1000}]
        primary["turns"][1]["model_calls"] = [{"started_at": 1500, "ttft_ms": 50}]
        complete, first = False, None
    elif scenario == "missing_call_start":
        primary["turns"][0]["model_calls"] = [{"ttft_ms": 20}]
        primary["turns"][1]["model_calls"] = [{"started_at": 1500, "ttft_ms": 50}]
        complete, first = False, None
    elif scenario == "model_first_zero":
        primary["turns"][0]["model_calls"] = [{"started_at": 1000, "ttft_ms": 0}]
        primary["turns"][1]["model_calls"] = [{"started_at": 1500, "ttft_ms": 50}]
    elif scenario == "session_count_mismatch":
        primary["summary"] = {"turns": 4}
        total, complete, counts_consistent = 4, False, False
    elif scenario == "total_count_mismatch":
        payload["meta"]["counts"]["turns"] = 4
        complete, counts_consistent = False, False
    elif scenario == "source_unknown":
        payload["meta"].pop("complete")
        complete = None
    elif scenario == "subagent":
        payload["sessions"].append({"role": "subagent", "turns": [{"timing": {}}]})
        payload["meta"]["counts"]["turns"] = 4
    calls = []

    async def handler(request):
        assert request.url.path.endswith("/trace/query")
        calls.append(request.url.path)
        return ok(payload)

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post("/v1/task-data/metrics", json={"task_id": "111"})
    assert response.status_code == 200
    result = response.json()
    assert result["timing_coverage"] == {
        "known_primary_turns": known,
        "primary_turns": total,
        "counts_consistent": counts_consistent,
    }
    assert result["values"]["total_latency_ms"] == union
    assert result["values"]["latency_ms_first_resp"] == first
    assert result["first_response_available"] is (first is not None)
    assert result["timing_complete"] is complete
    assert result["complete"] is complete
    assert len(calls) == 1


@pytest.mark.parametrize("scenario", ["complete", "missing_first", "missing_duration"])
def test_fornax_metrics_http_preserves_missing_timing(tmp_path, scenario):
    call = fornax_span()
    call["custom_tags"]["first_token_latency_ms"] = 0
    turn = {
        "span_id": "turn1",
        "trace_id": "t1",
        "span_name": "agent-turn(primary)",
        "started_at": 1000,
        "duration": 0,
        "custom_tags": {},
    }
    if scenario == "missing_first":
        call["custom_tags"].pop("first_token_latency_ms")
    elif scenario == "missing_duration":
        turn.pop("duration")
    calls = []

    async def handler(request):
        assert request.url.path.endswith("/observability/spans/search")
        calls.append(request.url.path)
        return ok({"spans": [call, turn], "has_more": False})

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/metrics", json={"task_id": "111", "source": "fornax"}
        )
    assert response.status_code == 200
    result = response.json()
    assert result["timing_complete"] is (scenario == "complete")
    assert result["values"]["latency_ms_first_resp"] == (
        None if scenario == "missing_first" else 0
    )
    assert result["values"]["total_latency_ms"] == (
        None if scenario == "missing_duration" else 0
    )
    assert result["timing_coverage"]["known_primary_turns"] == (
        0 if scenario == "missing_duration" else 1
    )
    assert len(calls) == 1


def token_summary_fixture():
    payload = trace_data()
    payload["meta"]["counts"]["model_calls"] = 509
    payload["summary"]["token_usage"] = {
        "input": 1527831,
        "output": 269719,
        "cached_input": 72456369,
        "total": 1797550,
        "known": True,
        "total_known": False,
        "total_source": "mixed",
    }
    return payload


def token_view_fixture(*, task_id="111", known=True, covered=508):
    return {
        "trace_detail": {
            "meta": {"complete": True, "scope": {"taskId": task_id}},
            "data": {
                "data": {
                    "totals": {
                        "tokenUsageKnown": known,
                        "tokenUsage": {
                            "input": 73984200,
                            "output": 269719,
                            "total": 74253919,
                            "cached_input": 72456369,
                            "reasoning_output": 0,
                        },
                    },
                    "mongoCoverage": {"knownModelCalls": covered, "modelCalls": 509},
                }
            },
        }
    }


@pytest.mark.parametrize(
    "scenario",
    [
        "view",
        "view_complete",
        "unknown",
        "zero",
        "failed_view",
        "foreign",
        "invalid",
        "unspecified_zero",
        "inconsistent_total",
    ],
)
def test_token_metrics_http_normalizes_cache_and_unknowns(tmp_path, scenario):
    calls = {"trace": 0, "task": 0}

    async def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/trace/query"):
            calls["trace"] += 1
            assert body["include_raw_spans"] is False
            payload = token_summary_fixture()
            if scenario in {"unknown", "zero", "unspecified_zero"}:
                payload["summary"]["token_usage"] = {
                    "input": 0,
                    "output": 0,
                    "cached_input": 0,
                    "total": 0,
                    "known": scenario == "zero",
                    "total_known": scenario == "zero",
                }
                if scenario == "unspecified_zero":
                    payload["summary"]["token_usage"].pop("known")
                    payload["summary"]["token_usage"].pop("total_known")
            if scenario == "invalid":
                payload["summary"]["token_usage"] = {
                    "input": True,
                    "output": "bad",
                    "cached_input": -1,
                    "known": True,
                    "total_known": True,
                }
            if scenario == "inconsistent_total":
                payload["summary"]["token_usage"] = {
                    "input": 100,
                    "output": 20,
                    "cached_input": 30,
                    "total": 999,
                    "known": True,
                    "total_known": True,
                }
            return ok(payload)
        if request.url.path == "/open-api/observability/spans/search":
            return httpx.Response(401, json={"code": 401})
        assert request.url.path == "/openapi/v1/task"
        calls["task"] += 1
        assert body.get("include_trace") is True or scenario == "failed_view"
        if scenario == "failed_view":
            return httpx.Response(503, json={"code": 9000, "data": {}})
        return ok(
            token_view_fixture(
                task_id="222" if scenario == "foreign" else "111",
                known=scenario
                not in {
                    "unknown",
                    "invalid",
                    "unspecified_zero",
                    "inconsistent_total",
                },
                covered=509 if scenario == "view_complete" else 508,
            )
        )

    with TestClient(
        create_app(config(tmp_path, max_retries=3), transport=transport(handler))
    ) as client:
        response = client.post("/v1/task-data/metrics", json={"task_id": "111"})
    assert response.status_code == 200
    data = response.json()
    values = data["values"]
    assert calls == {
        "trace": 1,
        "task": 0 if scenario == "zero" else 2 if scenario == "failed_view" else 1,
    }
    if scenario in {"view", "view_complete"}:
        assert values["input_tokens"] == 73984200
        assert values["cached_tokens"] == 72456369
        assert values["non_cached_input_tokens"] == 1527831
        assert values["output_tokens"] == 269719
        assert values["total_tokens"] == 74253919
        assert values["clean_total_token"] == 1797550
        assert data["token_source"] == "trace_view_totals"
        assert data["token_coverage"]["known_model_calls"] == (
            509 if scenario == "view_complete" else 508
        )
        assert data["tokens_complete"] is (scenario == "view_complete")
        assert data["turn_timing_complete"] is True
        assert data["first_response_available"] is False
        assert data["timing_complete"] is False
        assert data["complete"] is False
    elif scenario in {"unknown", "invalid", "unspecified_zero"}:
        assert all(
            values[key] is None
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_tokens",
                "clean_total_token",
                "reasoning_tokens",
            )
        )
        assert data["tokens_available"] is False
        assert data["complete"] is False
    elif scenario == "zero":
        assert all(
            values[key] == 0
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_tokens",
                "clean_total_token",
            )
        )
        assert data["tokens_complete"] is True
    elif scenario == "inconsistent_total":
        assert values["input_tokens"] == 100
        assert values["total_tokens"] is None
        assert values["clean_total_token"] == 90
        assert "TOKEN_TOTAL_INCONSISTENT" in data["token_diagnostics"]
        assert data["tokens_complete"] is False
    else:
        assert values["input_tokens"] is None
        assert values["total_tokens"] is None
        assert values["clean_total_token"] is None
        assert values["output_tokens"] == 269719
        assert data["token_source"] == "trace_summary"
        assert data["complete"] is False
    if scenario == "failed_view":
        assert (
            data["fallback_errors"]["token_view"]["code"]
            == "UPSTREAM_RETRIES_EXHAUSTED"
        )


@pytest.mark.parametrize(
    "components", [["trace", "metrics"], ["metrics", "conversation"]]
)
def test_export_token_metrics_reuse_conversation_view(tmp_path, components):
    calls = {"trace": 0, "task": 0}

    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            calls["trace"] += 1
            return ok(token_summary_fixture())
        assert request.url.path == "/openapi/v1/task"
        calls["task"] += 1
        payload = token_view_fixture()
        payload["trace_detail"]["data"]["messages"] = [{"content": "conversation"}]
        return ok(payload)

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/exports",
            json={
                "task_ids": ["111"],
                "components": components,
            },
        )
        job_id = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/v1/exports/{job_id}").json()
            if job["archive_ready"]:
                break
            time.sleep(0.01)
        metric = job["tasks"]["111"]["components"]["metrics"]
        stored_metric = json.loads((tmp_path / job_id / metric["file"]).read_text())
        values = stored_metric["values"]
        for field in (
            "timing_complete",
            "turn_timing_complete",
            "first_response_available",
            "timing_coverage",
            "timing_diagnostics",
        ):
            assert metric[field] == stored_metric[field]
        assert values["input_tokens"] == 73984200
        assert values["clean_total_token"] == 1797550
        assert metric["tokens_complete"] is False
        assert metric["token_source"] == "trace_view_totals"
    assert calls == {"trace": 1, "task": 1}


def task_token_detail(task_id="111", *, zero=False):
    return {
        "items": [
            {
                "id_kind": "task",
                "task_id": task_id,
                "task": {
                    "input_tokens": "0" if zero else "100",
                    "output_tokens": "0" if zero else "20",
                    "cached_tokens": "0" if zero else "30",
                    "total_tokens": "0" if zero else "120",
                    "total_execution_duration_ms": "999999",
                },
            }
        ],
        "page_info": {"has_more": False},
    }


@pytest.mark.parametrize("source", ["auto", "openapi"])
def test_timing_only_metrics_recover_tokens_independently(tmp_path, source):
    paths = []
    payload = trace_data()
    payload["summary"]["token_usage"] = {"known": False}

    async def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/trace/query"):
            return ok(payload)
        if request.url.path.endswith("/task"):
            return ok({})
        return ok({"spans": [fornax_span()], "has_more": False})

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post(
            "/v1/task-data/metrics",
            json={
                "task_id": "111",
                "source": source,
                "start_ms": 100,
                "end_ms": 5000,
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["values"]["total_latency_ms"] == 2500
    assert result["values"]["latency_ms_first_resp"] is None
    assert result["total_latency_available"] is True
    assert result["total_latency_complete"] is True
    assert result["first_response_available"] is False
    assert result["first_response_complete"] is False
    assert result["source"] == "flux_insight_openapi"
    assert result["tokens_complete"] is False
    if source == "auto":
        assert result["values"]["total_tokens"] == 12
        assert result["token_source"] == "fornax_usage"
        assert result["token_scope"]["start_ms"] == 100
        assert result["token_scope"]["end_ms"] == 5000
        assert result["token_sources"][0]["tokens_available"] is False
        assert result["token_sources"][1]["tokens_complete"] is True
        assert paths.count("/open-api/observability/spans/search") == 1
    else:
        assert result["values"]["total_tokens"] is None
        assert "/open-api/observability/spans/search" not in paths


@pytest.mark.parametrize("scenario", ["timing", "no_trace", "zero", "foreign"])
def test_task_accumulated_tokens_are_unconfirmed_fallback(tmp_path, scenario):
    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            if scenario == "no_trace":
                return httpx.Response(200, json={"code": 3004, "data": {}})
            payload = trace_data(complete=False)
            payload["summary"]["token_usage"] = {"known": False}
            return ok(payload)
        if request.url.path.endswith("/task"):
            return ok(
                task_token_detail(
                    "222" if scenario == "foreign" else "111", zero=scenario == "zero"
                )
            )
        return httpx.Response(401)

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        response = client.post("/v1/task-data/metrics", json={"task_id": "111"})
    assert response.status_code == 200
    result = response.json()
    assert result["fallback_errors"]["fornax"]["code"] == "FORNAX_UNAUTHORIZED"
    assert result["complete"] is False
    if scenario in {"zero", "foreign"}:
        assert result["tokens_available"] is False
        assert result["values"]["total_tokens"] is None
    else:
        assert result["values"]["total_tokens"] == 120
        assert result["tokens_complete"] is None
        assert result["token_source"] == "task_detail_totals"
        assert result["token_coverage"]["model_calls"] is None
        assert "TASK_TOTALS_NOT_TRACE_USAGE" in result["token_diagnostics"]
    if scenario == "no_trace":
        assert result["source"] == "task_detail"
        assert result["values"]["total_latency_ms"] is None
        assert result["values"]["model_call_count"] is None
        assert result["total_latency_available"] is False
    else:
        assert result["values"]["total_latency_ms"] == 2500
        assert result["total_latency_complete"] is False
    assert result["values"]["latency_ms_first_resp"] is None


def test_fornax_partial_usage_and_read_errors_survive_supplement(tmp_path):
    pages = 0

    async def handler(request):
        nonlocal pages
        if request.url.path.endswith("/trace/query"):
            payload = trace_data()
            payload["summary"]["token_usage"] = {"known": False}
            return ok(payload)
        if request.url.path.endswith("/task"):
            return ok({})
        pages += 1
        if pages == 2:
            return httpx.Response(401)
        missing = fornax_span("missing")
        missing["custom_tags"] = {}
        return ok(
            {
                "spans": [fornax_span(), missing],
                "has_more": True,
                "next_page_token": "next",
            }
        )

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        result = client.post("/v1/task-data/metrics", json={"task_id": "111"}).json()
    assert pages == 2
    assert result["values"]["total_tokens"] == 12
    assert result["values"]["total_latency_ms"] == 2500
    assert result["token_coverage"] == {"known_model_calls": 1, "model_calls": 2}
    assert result["tokens_complete"] is False
    assert "TOKEN_CALL_COVERAGE_INCOMPLETE" in result["token_diagnostics"]
    assert result["fallback_errors"]["fornax"]["code"] == "FORNAX_UNAUTHORIZED"
    assert result["token_sources"][-1]["token_scope"]["pagination_complete"] is False


def test_token_source_conflicts_preserve_original_values_and_evidence(tmp_path):
    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            payload = trace_data()
            payload["summary"]["token_usage"].update(output=None, total_known=False)
            return ok(payload)
        if request.url.path.endswith("/task"):
            return ok(task_token_detail())
        return ok({"spans": [fornax_span()], "has_more": False})

    with TestClient(
        create_app(config(tmp_path), transport=transport(handler))
    ) as client:
        result = client.post("/v1/task-data/metrics", json={"task_id": "111"}).json()
    assert result["values"]["input_tokens"] == 100
    assert result["values"]["output_tokens"] == 20
    assert result["values"]["total_tokens"] == 120
    assert [s["token_source"] for s in result["token_sources"]] == [
        "trace_summary",
        "fornax_usage",
        "task_detail_totals",
    ]
    assert result["token_sources"][1]["values"]["input_tokens"] == 10
    assert result["token_field_sources"]["input_tokens"] == "trace_summary"
    assert result["token_field_sources"]["total_tokens"] == "task_detail_totals"
    assert result["tokens_complete"] is not True


def test_view_usage_coverage_does_not_hide_execution_gaps():
    from flux_task_service.token_usage import view_tokens

    data = token_view_fixture(covered=509)
    data["trace_detail"]["meta"]["coverage"] = [
        {"source": "execution", "required": True, "status": "error"},
    ]
    normalized = view_tokens(data, "111")
    assert normalized["values"]["total_tokens"] == 74253919
    assert normalized["tokens_complete"] is False
    assert normalized["token_scope"]["coverage"][0]["status"] == "error"


async def test_fornax_auth_update_reload_and_file_rotation(tmp_path):
    from flux_task_service.resources import FornaxClient

    jwt_file = tmp_path / "jwt"
    jwt_file.write_text("file-old")
    cfg = config(tmp_path, fornax_jwt=None, fornax_jwt_file=jwt_file)
    async with httpx.AsyncClient() as http:
        client = FornaxClient(cfg, http)
        assert await client.auth_token() == "file-old"
        await client.update_token("memory-new")
        assert await client.auth_token() == "memory-new"
        jwt_file.write_text("file-new")
        assert await client.auth_token() == "file-new"
        await client.update_token("memory-newer")
        assert await client.auth_token(reload=True) == "file-new"


def expired_personal_jwt():
    import base64

    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": 1}).encode()).decode().rstrip("=")
    )
    return f"header.{payload}.signature"


def test_auth_endpoint_expiry_update_and_explicit_refresh(tmp_path, monkeypatch):
    from flux_task_service.resources import FornaxClient

    monkeypatch.setattr(
        FornaxClient, "chrome_cookies", staticmethod(lambda: "CAS_SESSION=mock")
    )
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/auth/api/v1/jwt":
            assert request.headers["Cookie"] == "CAS_SESSION=mock"
            assert "Authorization" not in request.headers
            return httpx.Response(200, headers={"X-Jwt-Token": "refreshed-personal"})
        assert request.headers["X-Jwt-Token"] == "updated-personal"
        return ok({"spans": [fornax_span()], "has_more": False})

    cfg = config(
        tmp_path,
        fornax_jwt=SecretStr(expired_personal_jwt()),
        api_key=SecretStr("caller-key"),
    )
    with TestClient(create_app(cfg, transport=httpx.MockTransport(handler))) as client:
        endpoint = "/v1/task-data/fornax/auth"
        assert client.post(endpoint, json={}).status_code == 401
        client.headers["Authorization"] = "Bearer caller-key"
        expired = client.post(
            "/v1/task-data/metrics", json={"task_id": "111", "source": "fornax"}
        )
        assert expired.status_code == 502
        assert (
            expired.json()["error"]["fallback_errors"]["fornax"]["code"]
            == "FORNAX_TOKEN_EXPIRED"
        )
        assert requests == []
        invalid = client.post(
            endpoint, json={"action": "update", "jwt": expired_personal_jwt()}
        )
        assert invalid.status_code == 422
        assert expired_personal_jwt() not in invalid.text
        updated = client.post(
            endpoint, json={"action": "update", "jwt": "updated-personal"}
        )
        assert updated.status_code == 200
        assert updated.json()["upstream_verified"] is False
        assert "updated-personal" not in updated.text
        assert (
            client.post(
                "/v1/task-data/metrics", json={"task_id": "111", "source": "fornax"}
            ).status_code
            == 200
        )
        assert client.post(endpoint, json={"action": "refresh"}).status_code == 200
        assert cfg.fornax_refresh_from_chrome is False
        assert (
            client.post(endpoint, json={"action": "reload"}).json()["error"]["code"]
            == "FORNAX_TOKEN_EXPIRED"
        )
        for body in ({"action": "update"}, {"action": "reload", "jwt": "secret"}):
            assert client.post(endpoint, json=body).status_code == 422


def test_export_reuses_saved_detail_and_keeps_fallback_evidence(tmp_path):
    calls = {"detail": 0, "view": 0}

    async def handler(request):
        if request.url.path.endswith("/trace/query"):
            return httpx.Response(200, json={"code": 3004, "data": {}})
        assert request.url.path.endswith("/task")
        body = json.loads(request.content)
        calls["view" if body.get("include_trace") else "detail"] += 1
        return ok(task_token_detail())

    cfg = config(tmp_path, fornax_jwt=SecretStr(expired_personal_jwt()))
    with TestClient(create_app(cfg, transport=transport(handler))) as client:
        response = client.post(
            "/v1/exports", json={"task_ids": ["111"], "components": ["task", "metrics"]}
        )
        job_id = response.json()["job_id"]
        for _ in range(200):
            job = client.get(f"/v1/exports/{job_id}").json()
            if job["archive_ready"]:
                break
            time.sleep(0.01)
        component = job["tasks"]["111"]["components"]["metrics"]
        assert component["status"] == "partial"
        reader = TaskDataReader(tmp_path / job_id / "tasks/111")
        reader.validate()
        metric = reader.read_dataset("metrics")["documents"][0]["content"]
        assert metric["token_source"] == "task_detail_totals"
        assert metric["values"]["total_tokens"] == 120
        assert metric["fallback_errors"]["fornax"]["code"] == "FORNAX_TOKEN_EXPIRED"
        for key in (
            "token_sources",
            "total_latency_available",
            "first_response_complete",
        ):
            assert component[key] == metric[key]
    assert calls == {"detail": 1, "view": 0}
