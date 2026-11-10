"""Retry exports with stable selection, in-place replacement and authorization."""

import asyncio
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
from flux_task_service.errors import ServiceError
from flux_task_service.exports import ExportManager
from flux_task_service.models import ExportQuery, ExportRetryQuery
from flux_task_service.resources import ResourceService
from flux_task_service.service import TaskService

SOURCE_ID = "a" * 32


def config(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        export_dir=tmp_path,
        max_retries=0,
        service_secret=SecretStr("test-secret"),
        **overrides,
    )


def source_job(tmp_path, *, request=True, task_ids=True):
    root = tmp_path / SOURCE_ID
    root.mkdir()
    job = {
        "job_id": SOURCE_ID,
        "status": "partial",
        "archive_ready": True,
        "task_count": 2,
        "components": ["task"],
        "tasks": {tid: {"status": "error", "components": {}} for tid in ("111", "222")},
    }
    if request:
        job["request"] = ExportQuery(
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
            conversation_mode="messages",
            include_raw_spans=True,
            recover_raw_spans=True,
            start_ms=100,
            end_ms=200,
        ).model_dump(mode="json")
    (root / "job.json").write_text(json.dumps(job))
    if task_ids:
        (root / "task_ids.json").write_text('["222", "111"]')
    (root / "export.zip").write_bytes(b"original-archive")
    return job


def upstream(calls):
    def handle(request):
        if request.url.path == "/auth/api/v1/jwt":
            return httpx.Response(200, headers={"X-Jwt-Token": "test-jwt"})
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        assert "filters" not in body, (
            "Retry must not rerun the original filter selection"
        )
        if request.url.path.endswith("/versions"):
            data = {"versions": [], "comparisons": []}
        else:
            assert request.url.path == "/openapi/v1/task"
            data = {
                "page_info": {"has_more": False},
                "items": [
                    {
                        "task_id": body["task_id_list"][0],
                        "id_kind": "task",
                        "task": {"title": "newly fetched"},
                    }
                ],
            }
        return httpx.Response(200, json={"code": 0, "data": data})

    return httpx.MockTransport(handle)


@pytest.mark.parametrize("task_id", [None, "111"])
async def test_retry_fetches_fixed_scope_and_overwrites_same_batch(tmp_path, task_id):
    source_job(tmp_path)
    root = tmp_path / SOURCE_ID
    for tid in ("111", "222"):
        task_root = root / "tasks" / tid
        task_root.mkdir(parents=True)
        (task_root / "task.json").write_text('{"old": true}')
        (task_root / "obsolete.json").write_text('"obsolete"')
    untouched = {p.name: p.read_bytes() for p in (root / "tasks" / "222").iterdir()}
    calls = []
    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=upstream(calls)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        child = manager.retry(SOURCE_ID, ExportRetryQuery(), task_id=task_id)
        assert child["job_id"] == SOURCE_ID
        running = manager.running[SOURCE_ID]
        duplicate = manager.retry(SOURCE_ID, ExportRetryQuery(), task_id=task_id)
        assert duplicate is child
        assert manager.running[SOURCE_ID] is running
        assert not child["archive_ready"]
        await running
        final = manager.get(child["job_id"])
    expected = ["111"] if task_id else ["222", "111"]
    assert final["status"] == ("partial" if task_id else "completed")
    assert final["task_count"] == final["completed_count"] == 2
    assert final["request"]["task_ids"] == ["222", "111"]
    assert final["retry_request"]["task_ids"] == expected
    assert final["request"]["filters"] is None
    assert final["request"]["conversation_mode"] == "messages"
    assert final["request"]["include_raw_spans"] is True
    assert final["request"]["recover_raw_spans"] is True
    assert final["request"]["start_ms"] == 100
    assert final["request"]["end_ms"] == 200
    assert final["retry_of"]["job_id"] == SOURCE_ID
    assert final["retry_of"]["scope"] == ("task" if task_id else "batch")
    assert [body["task_id_list"][0] for _, body in calls] == expected
    assert [p.name for p in tmp_path.iterdir()] == [SOURCE_ID]
    assert json.loads((root / "task_ids.json").read_bytes()) == ["222", "111"]
    for tid in expected:
        assert not (root / "tasks" / tid / "obsolete.json").exists()
    if task_id:
        assert {
            p.name: p.read_bytes() for p in (root / "tasks" / "222").iterdir()
        } == untouched
    with zipfile.ZipFile(tmp_path / child["job_id"] / "export.zip") as archive:
        assert json.loads(archive.read("job.json"))["retry_of"] == final["retry_of"]
        for tid in expected:
            assert "newly fetched" in archive.read(f"tasks/{tid}/task.json").decode()
            assert f"tasks/{tid}/obsolete.json" not in archive.namelist()
        if task_id:
            assert archive.read("tasks/222/task.json") == untouched["task.json"]
    excluded = {"job.json", "export.zip"}
    assert final["bytes_written"] == sum(
        p.stat().st_size
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() not in excluded
    )


async def test_legacy_retry_defaults_are_explicit_and_can_be_overridden(tmp_path):
    source_job(tmp_path, request=False)
    cfg = config(tmp_path)
    calls = []
    async with httpx.AsyncClient(transport=upstream(calls)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        child = manager.retry(
            SOURCE_ID,
            ExportRetryQuery(components=["versions"], conversation_mode="messages"),
        )
        await manager.running[child["job_id"]]
    assert child["warnings"]
    assert child["retry_of"]["parameters"] == "legacy_defaults"
    assert child["request"]["conversation_mode"] == "messages"
    assert child["request"]["include_raw_spans"] is False
    assert child["components"] == ["versions"]
    assert all(path.endswith("/versions") for path, _ in calls)


def test_retry_override_validation_and_empty_scope(tmp_path):
    job = source_job(tmp_path)
    cfg = config(tmp_path)
    manager = ExportManager(None, None, cfg, asyncio.Semaphore(4))
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery(include_raw_spans=False))
    assert exc.value.code == "INVALID_QUERY"  # Inherited recover=true requires raw.
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery(start_ms=201))
    assert exc.value.status_code == 422
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery(), task_id="333")
    assert exc.value.code == "EXPORT_TASK_NOT_FOUND"
    manager.running[SOURCE_ID] = object()
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "EXPORT_STILL_RUNNING"
    manager.running.clear()
    (tmp_path / SOURCE_ID / "task_ids.json").write_text("[]")
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "EXPORT_RETRY_EMPTY"
    (tmp_path / SOURCE_ID / "task_ids.json").unlink()
    job["task_count"] = 3
    (tmp_path / SOURCE_ID / "job.json").write_text(json.dumps(job))
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "EXPORT_RETRY_SCOPE_UNAVAILABLE"


def test_retry_endpoints_authorization_empty_body_and_restart(tmp_path):
    source_job(tmp_path)
    cfg = config(tmp_path, api_key=SecretStr("caller-key"))
    headers = {"Authorization": "Bearer caller-key"}
    paths = [
        f"/v1/exports/{SOURCE_ID}/retry",
        f"/v1/exports/{SOURCE_ID}/tasks/111/retry",
    ]
    calls = []
    with TestClient(create_app(cfg, transport=upstream(calls))) as client:
        for path in paths:
            assert client.post(path).status_code == 401
        response = client.post(paths[1], headers=headers)  # No body means inherit.
        assert response.status_code == 202
        child = response.json()
        for _ in range(100):
            result = client.get(
                f"/v1/exports/{child['job_id']}", headers=headers
            ).json()
            if result["archive_ready"]:
                break
            time.sleep(0.01)
        assert result["archive_ready"]
        assert (
            client.post(paths[0], headers=headers, json={"components": []}).status_code
            == 422
        )
        assert (
            client.post(
                paths[0], headers=headers, json={"task_ids": ["333"]}
            ).status_code
            == 422
        )
        assert (
            client.post(f"/v1/exports/{'b' * 32}/retry", headers=headers).status_code
            == 404
        )
    with TestClient(create_app(cfg, transport=upstream(calls))) as restarted:
        result = restarted.get(f"/v1/exports/{child['job_id']}", headers=headers).json()
        assert result["job_id"] == SOURCE_ID
        assert result["request"]["task_ids"] == ["222", "111"]
        assert result["retry_of"]["job_id"] == SOURCE_ID


def test_export_status_can_omit_page_and_select_tasks(tmp_path):
    source_job(tmp_path)
    with TestClient(create_app(config(tmp_path))) as client:
        full = client.get(f"/v1/exports/{SOURCE_ID}").json()
        assert list(full["tasks"]) == ["111", "222"]
        assert full["tasks_total"] == 2
        assert full["has_more_tasks"] is False

        summary = client.get(
            f"/v1/exports/{SOURCE_ID}", params={"include_tasks": "false"}
        ).json()
        assert "tasks" not in summary
        assert summary["tasks_total"] == 2

        first = client.get(
            f"/v1/exports/{SOURCE_ID}", params={"offset": 0, "limit": 1}
        ).json()
        assert list(first["tasks"]) == ["111"]
        assert first["tasks_offset"] == 0
        assert first["tasks_limit"] == 1
        assert first["has_more_tasks"] is True

        second = client.get(
            f"/v1/exports/{SOURCE_ID}", params={"offset": 1, "limit": 1}
        ).json()
        assert list(second["tasks"]) == ["222"]
        assert second["has_more_tasks"] is False

        selected = client.get(
            f"/v1/exports/{SOURCE_ID}", params={"task_id": "222"}
        ).json()
        assert list(selected["tasks"]) == ["222"]

        missing = client.get(f"/v1/exports/{SOURCE_ID}", params={"task_id": "333"})
        assert missing.status_code == 404


async def test_retry_scope_integrity_limits_and_override_clearing(
    tmp_path, monkeypatch
):
    source_job(tmp_path)
    cfg = config(tmp_path, max_task_ids=1, max_export_jobs=1)
    manager = ExportManager(None, None, cfg, asyncio.Semaphore(4))
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "QUERY_LIMIT_EXCEEDED"
    manager.running["b" * 32] = object()
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery(), task_id="111")
    assert exc.value.code == "EXPORT_BUSY"
    manager.running.clear()
    captured = {}

    async def capture(job, query, **kwargs):
        captured.update(query=query, **kwargs)

    monkeypatch.setattr(manager, "run", capture)
    manager.retry(
        SOURCE_ID,
        ExportRetryQuery(
            start_ms=None,
            end_ms=None,
            include_raw_spans=False,
            recover_raw_spans=False,
        ),
        task_id="111",
    )
    await manager.running.pop(SOURCE_ID)
    manager.jobs.clear()
    assert captured["query"].start_ms is None
    assert captured["query"].end_ms is None
    assert captured["query"].include_raw_spans is False
    assert captured["query"].recover_raw_spans is False
    (tmp_path / SOURCE_ID / "task_ids.json").write_text('["111"]')
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "EXPORT_RETRY_SCOPE_UNAVAILABLE"


async def test_retry_fallback_uses_saved_ids_for_unstarted_cancelled_export(
    tmp_path, monkeypatch
):
    job = source_job(tmp_path, task_ids=False)
    job.update(status="cancelled", tasks={})
    job["request"] = ExportQuery(
        task_ids=["111", "222"], components=["task"]
    ).model_dump(mode="json")
    (tmp_path / SOURCE_ID / "job.json").write_text(json.dumps(job))
    manager = ExportManager(None, None, config(tmp_path), asyncio.Semaphore(4))

    async def capture(job, query, **kwargs):
        pass

    monkeypatch.setattr(manager, "run", capture)
    result = manager.retry(SOURCE_ID, ExportRetryQuery())
    assert result["request"]["task_ids"] == ["111", "222"]
    await manager.running[SOURCE_ID]


@pytest.mark.parametrize("saved_request", [True, False])
async def test_sequential_retries_keep_batch_scope_and_task_parameters(
    tmp_path, saved_request
):
    source_job(tmp_path, request=saved_request)
    calls = []
    previous_size = None
    cfg = config(tmp_path)
    async with httpx.AsyncClient(transport=upstream(calls)) as http:
        api = OpenAPIClient(cfg, http)
        manager = ExportManager(
            ResourceService(api, cfg), TaskService(api, cfg), cfg, asyncio.Semaphore(4)
        )
        for overrides, task_id in [
            (ExportRetryQuery(components=["versions"]), "111"),
            (ExportRetryQuery(), "111"),
            (ExportRetryQuery(), None),
            (ExportRetryQuery(), None),
        ]:
            job = manager.retry(SOURCE_ID, overrides, task_id=task_id)
            assert job["job_id"] == SOURCE_ID
            await manager.running[SOURCE_ID]
            final = manager.get(SOURCE_ID)
            assert final["task_count"] == 2
            assert final["request"]["components"] == ["task"]
            assert final["request"]["task_ids"] == ["222", "111"]
            if task_id:
                assert final["task_requests"]["111"]["components"] == ["versions"]
                # Simulate a historical missing ID file: fall back to the full batch.
                (tmp_path / SOURCE_ID / "task_ids.json").unlink()
            else:
                assert final["task_requests"] == {}
                assert final["components"] == ["task"]
                assert final["status"] == "completed"
                assert final["completed_count"] == 2
                assert not (tmp_path / SOURCE_ID / "tasks/111/versions.json").exists()
                current_size = final["bytes_written"]
                if previous_size is not None:
                    assert current_size == previous_size
                previous_size = current_size
    assert len(calls) == 6
    assert all(path.endswith("/versions") for path, _ in calls[:2])
    assert [body["task_id_list"][0] for _, body in calls[2:]] == [
        "222",
        "111",
        "222",
        "111",
    ]
    assert [p.name for p in tmp_path.iterdir()] == [SOURCE_ID]


@pytest.mark.parametrize("cancel", [False, True])
async def test_retry_failure_or_cancellation_does_not_publish_old_task_data(
    tmp_path, cancel
):
    source_job(tmp_path)
    root = tmp_path / SOURCE_ID
    task_root = root / "tasks/111"
    task_root.mkdir(parents=True)
    (task_root / "task.json").write_text('{"old": true}')
    cfg = config(tmp_path)

    class FailingResources:
        async def task(self, task_id):
            raise ServiceError("UPSTREAM_UNAVAILABLE", "Unavailable", 503)

    manager = ExportManager(FailingResources(), None, cfg, asyncio.Semaphore(4))
    job = manager.retry(SOURCE_ID, ExportRetryQuery(), task_id="111")
    with pytest.raises(ServiceError) as exc:
        manager.retry(SOURCE_ID, ExportRetryQuery())
    assert exc.value.code == "EXPORT_STILL_RUNNING"
    with pytest.raises(ServiceError) as exc:
        manager.retry(
            SOURCE_ID, ExportRetryQuery(components=["versions"]), task_id="111"
        )
    assert exc.value.code == "EXPORT_STILL_RUNNING"
    # The old ZIP remains on disk until replacement but must not become available
    # just because a new manager sees an interrupted retry.
    restarted = ExportManager(None, None, cfg, asyncio.Semaphore(4))
    interrupted = restarted.get(SOURCE_ID)
    assert interrupted["status"] == "interrupted"
    assert interrupted["archive_ready"] is False
    if cancel:
        manager.cancel(SOURCE_ID)
    await manager.running[SOURCE_ID]
    final = manager.get(SOURCE_ID)
    assert final["job_id"] == job["job_id"] == SOURCE_ID
    assert final["status"] == ("cancelled" if cancel else "partial")
    assert final["archive_ready"] is True
    assert not (task_root / "task.json").exists()
    with zipfile.ZipFile(root / "export.zip") as archive:
        assert "tasks/111/task.json" not in archive.namelist()
        assert json.loads(archive.read("task_ids.json")) == ["222", "111"]
        manifest = json.loads(archive.read("tasks/111/data-manifest.json"))
        assert manifest["task_status"] == ("cancelled" if cancel else "error")
