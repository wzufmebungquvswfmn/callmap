"""Batch browsing and saved documents must not trigger upstream acquisitions."""

import asyncio
import json

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.config import Settings
from flux_task_service.export_contract import build_manifest

JOB = "a" * 32


def settings(tmp_path, **values):
    return Settings(_env_file=None, export_dir=tmp_path, **values)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def saved(tmp_path):
    root = tmp_path / JOB / "tasks" / "111"
    write(
        root / "task.json",
        {"items": [{"task_id": "111", "task": {"title": "保存时的标题"}}]},
    )
    write(root / "artifacts/manifest.json", {"files": [{"path": "src/a.txt"}]})
    file = root / "artifacts/files/src/a.txt"
    file.parent.mkdir(parents=True)
    file.write_text("saved file")
    write(
        root / "artifacts/downloads.json",
        [
            {
                "path": "src/a.txt",
                "status": "ok",
                "file": "tasks/111/artifacts/files/src/a.txt",
            }
        ],
    )
    components = {
        name: {"status": "ok", "file": f"tasks/111/{path}"}
        for name, path in (
            ("task", "task.json"),
            ("artifacts/manifest", "artifacts/manifest.json"),
            ("artifacts", "artifacts/downloads.json"),
        )
    }
    manifest = build_manifest(
        root,
        "111",
        ["task", "artifacts"],
        components,
        finalized=True,
        task_status="ok",
        raw_requested=False,
    )
    write(root / "data-manifest.json", manifest.model_dump())
    job = {
        "job_id": JOB,
        "status": "completed",
        "created_at": 1,
        "archive_ready": True,
        "task_count": 1,
        "completed_count": 1,
        "request": {"filters": {"private": "not in overview"}},
        "tasks": {"111": {"status": "ok", "components": components}},
    }
    write(tmp_path / JOB / "job.json", job)
    return job, root


def no_upstream(request):
    raise AssertionError(f"Saved browsing called upstream: {request.url.path}")


def test_saved_data_and_file_are_manifest_owned_and_never_live(tmp_path):
    _, root = saved(tmp_path)
    with TestClient(
        create_app(settings(tmp_path), transport=httpx.MockTransport(no_upstream))
    ) as client:
        base = f"/v1/exports/{JOB}/tasks/111"
        data = client.get(base + "/datasets/task").json()
        assert (
            data["documents"][0]["content"]["items"][0]["task"]["title"]
            == "保存时的标题"
        )
        assert client.get(base + "/datasets/gui").json()["status"] == "not_requested"
        assert (
            client.get(
                base + "/file", params={"path": "artifacts/files/src/a.txt"}
            ).text
            == "saved file"
        )
        assert (
            client.get(base + "/file", params={"path": "../../job.json"}).status_code
            == 404
        )
        assert (
            client.get(base + "/file", params={"path": "task.json"}).status_code == 404
        )
        target = root / "artifacts/files/src/a.txt"
        target.unlink()
        outside = tmp_path / "outside.txt"
        outside.write_text("private")
        target.symlink_to(outside)
        assert (
            client.get(
                base + "/file", params={"path": "artifacts/files/src/a.txt"}
            ).status_code
            == 404
        )
        assert (
            client.get(f"/v1/exports/{JOB}/tasks/222/datasets/task").status_code == 404
        )


def test_history_overview_paging_and_in_progress_snapshot(tmp_path):
    job, _ = saved(tmp_path)
    job.update(
        status="running", task_count=125, completed_count=124, archive_ready=False
    )
    job["tasks"].update(
        {str(200 + n): {"status": "ok", "components": {}} for n in range(124)}
    )
    job["tasks"]["111"].update(
        status="running",
        progress={
            "stage": "artifacts",
            "file_count": 795,
            "completed_count": 721,
            "failed_count": 1,
        },
    )
    with TestClient(
        create_app(settings(tmp_path), transport=httpx.MockTransport(no_upstream))
    ) as client:
        client.app.state.exports.jobs[JOB] = job
        history = client.get("/v1/exports").json()
        assert history["total"] == 1
        assert history["jobs"][0]["status"] == "running"
        overview = client.get(f"/v1/exports/{JOB}/overview").json()
        assert "tasks" not in overview and "request" not in overview
        assert overview["task_counts"]["ok"] == 124
        assert overview["file_progress"]["completed_count"] == 721
        page = client.get(f"/v1/exports/{JOB}/tasks?offset=50&limit=50").json()
        assert len(page["tasks"]) == 50 and page["total"] == 125
        assert "111" not in page["tasks"]
        filtered = client.get(f"/v1/exports/{JOB}/tasks?status=active").json()
        assert list(filtered["tasks"]) == ["111"]
        assert (
            client.get(f"/v1/exports/{JOB}/tasks/111/datasets/task").status_code == 409
        )
        job["tasks"]["111"].update(
            status="partial", components={"gui": {"status": "error"}}
        )
        assert (
            client.get(f"/v1/exports/{JOB}/tasks?status=issues&search=111").json()[
                "total"
            ]
            == 1
        )
        client.app.state.exports.jobs.clear()


def test_summaries_use_one_batch_and_project_only_display_fields(tmp_path):
    calls = []

    def upstream(request):
        if "jwt" in request.url.path:
            return httpx.Response(200, headers={"X-Jwt-Token": "test"})
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "items": [
                        {
                            "id_kind": "task",
                            "task_id": tid,
                            "task": {
                                "title": "title",
                                "created_by": "owner",
                                "private_field": "hidden",
                            },
                        }
                        for tid in ("111", "222")
                    ],
                    "page_info": {"has_more": False},
                },
            },
        )

    config = settings(
        tmp_path, service_secret=SecretStr("test"), api_key=SecretStr("key")
    )
    with TestClient(
        create_app(config, transport=httpx.MockTransport(upstream))
    ) as client:
        assert (
            client.post("/v1/tasks/summaries", json={"task_ids": ["111"]}).status_code
            == 401
        )
        data = client.post(
            "/v1/tasks/summaries",
            json={"task_ids": ["111", "222", "111"]},
            headers={"Authorization": "Bearer key"},
        ).json()
        assert len(calls) == 1 and calls[0]["task_id_list"] == ["111", "222"]
        assert data["missing"] == [] and data["tasks"]["111"]["owner"] == "owner"
        assert "private_field" not in str(data)
        for path in [
            "/v1/exports",
            f"/v1/exports/{JOB}/overview",
            f"/v1/exports/{JOB}/tasks",
            f"/v1/exports/{JOB}/tasks/111/datasets/task",
            f"/v1/exports/{JOB}/tasks/111/file?path=a",
        ]:
            assert client.get(path).status_code == 401


def test_summary_timeout_is_a_recoverable_api_error(tmp_path):
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200)

    config = settings(
        tmp_path, service_secret=SecretStr("test"), query_timeout_seconds=0.01
    )
    with TestClient(create_app(config, transport=httpx.MockTransport(slow))) as client:
        response = client.post("/v1/tasks/summaries", json={"task_ids": ["111"]})
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "QUERY_TIMEOUT"
