"""Service integration: authenticated, durable jobs over existing exports."""

import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.config import Settings
from flux_task_service.export_contract import DATASETS
from flux_task_service.tagging import service as tagging_service

EXPORT_ID = "a" * 32
HEADERS = {"Authorization": "Bearer caller-key"}


def create_tagging(client):
    response = client.post(f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={})
    assert response.status_code == 202
    identity = response.json()["tagging_id"]
    assert wait_for_job(client, identity)["archive_ready"] is True
    return identity


def test_download_selected_tasks_includes_current_tags_and_scoped_counts(tmp_path):
    seed_export(tmp_path, task_ids=("111", "222", "333"))
    with TestClient(create_app(settings(tmp_path))) as client:
        created = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging",
            headers=HEADERS,
            json={"policy": {"require_problem_signal": True}},
        )
        identity = created.json()["tagging_id"]
        assert wait_for_job(client, identity)["archive_ready"] is True
        tag_url = f"/v1/tagging/{identity}/task/222"
        current = client.get(tag_url, headers=HEADERS)
        tags = {"review": {"source": "human", "decision": "hold", "reason": "需复核"}}
        assert (
            client.post(
                tag_url,
                headers={**HEADERS, "If-Match": current.headers["ETag"]},
                json=tags,
            ).status_code
            == 200
        )
        url = f"/v1/tagging/{identity}/download"
        # Repeat parameters for a batch, deduplicating in original batch order.
        response = client.get(
            url,
            headers=HEADERS,
            params=[
                ("task_id", "222"),
                ("task_id", "111"),
                ("task_id", "222"),
            ],
        )
        assert response.status_code == 200
        with zipfile.ZipFile(BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {
                "index.json",
                "selected-tasks.json",
                "tasks/111.json",
                "tasks/222.json",
            }
            index = json.loads(archive.read("index.json"))
            assert index["task_count"] == 2
            assert index["decision_counts"] == {"keep": 1, "exclude": 1}
            assert [r["task_id"] for r in index["records"]] == ["111", "222"]
            assert index["records"][1]["dynamic_tags"] == tags
            task = json.loads(archive.read("tasks/222.json"))
            assert task["dynamic_tags"] == tags and task["dynamic_tag_revision"] == 1
            selected = json.loads(archive.read("selected-tasks.json"))
            assert [r["task_id"] for r in selected] == ["111"]
        single = client.get(url, headers=HEADERS, params={"task_id": "222"})
        with zipfile.ZipFile(BytesIO(single.content)) as archive:
            assert json.loads(archive.read("selected-tasks.json")) == []
            assert json.loads(archive.read("index.json"))["task_count"] == 1
        # Filtering a download must not mutate the batch or later full downloads.
        complete = client.get(url, headers=HEADERS)
        with zipfile.ZipFile(BytesIO(complete.content)) as archive:
            assert "tasks/333.json" in archive.namelist()
            index = json.loads(archive.read("index.json"))
            assert index["task_count"] == 3
            assert index["decision_counts"] == {"keep": 1, "exclude": 2}
        assert (
            client.get(f"/v1/tagging/{identity}/results", headers=HEADERS).json()[
                "total"
            ]
            == 3
        )
        assert not list((tmp_path / "_tagging" / identity).glob("download-*.zip"))


def test_download_selection_rejects_invalid_or_missing_task_ids(tmp_path):
    seed_export(tmp_path)
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        identity = create_tagging(client)
        url = f"/v1/tagging/{identity}/download"
        for invalid in ("", "0", "abc", "../111", "111,222", "1" * 65):
            response = client.get(url, headers=HEADERS, params={"task_id": invalid})
            assert response.status_code == 422
        missing = client.get(
            url,
            headers=HEADERS,
            params=[
                ("task_id", "111"),
                ("task_id", "999"),
            ],
        )
        assert missing.status_code == 404
        assert client.get(url, params={"task_id": "111"}).status_code == 401
        app.state.tagging.settings.max_task_ids = 1
        excessive = client.get(
            url,
            headers=HEADERS,
            params=[
                ("task_id", "111"),
                ("task_id", "111"),
            ],
        )
        assert excessive.status_code == 422
        assert excessive.json()["error"]["code"] == "QUERY_LIMIT_EXCEEDED"
        assert not list((tmp_path / "_tagging" / identity).glob("download-*.zip"))


def test_dynamic_tag_replacement_roundtrip_exports_and_restart(tmp_path):
    seed_export(tmp_path, task_ids=("111", "222"))
    config = settings(tmp_path)
    with TestClient(create_app(config)) as client:
        identity = create_tagging(client)
        url = f"/v1/tagging/{identity}/task/111"
        schema = client.get("/v1/tagging/tag-schema", headers=HEADERS)
        assert schema.status_code == 200
        assert set(schema.json()["schema"]["properties"]) == {
            "repository",
            "task_classification",
            "review",
        }
        first = client.get(url, headers=HEADERS)
        assert first.status_code == 200 and first.headers["ETag"]
        assert first.json()["repository"]["source"] == "rule"
        assert first.headers["cache-control"] == "no-store"
        assert client.post(url, headers=HEADERS, json={}).status_code == 428
        assert client.get(url, headers=HEADERS).headers["ETag"] == first.headers["ETag"]
        original = client.get(
            f"/v1/tagging/{identity}/tasks/111", headers=HEADERS
        ).json()
        payload = {
            **first.json(),
            "task_classification": {"source": "model", "category": "代码审查"},
            "review": {"source": "human", "decision": "hold", "reason": "待核查"},
        }
        updated = client.post(
            url, headers={**HEADERS, "If-Match": first.headers["ETag"]}, json=payload
        )
        assert updated.status_code == 200 and updated.json() == payload
        assert updated.headers["ETag"] != first.headers["ETag"]
        conflict = client.post(
            url, headers={**HEADERS, "If-Match": first.headers["ETag"]}, json={}
        )
        assert conflict.status_code == 412
        assert conflict.json()["error"]["code"] == "TAGGING_REVISION_CONFLICT"
        invalid = client.post(
            url,
            headers={**HEADERS, "If-Match": updated.headers["ETag"]},
            json={"admission": {"decision": "keep"}},
        )
        assert invalid.status_code == 422
        assert client.get(url, headers=HEADERS).json() == payload
        detail = client.get(f"/v1/tagging/{identity}/tasks/111", headers=HEADERS).json()
        assert detail["dynamic_tags"] == payload
        assert detail["dynamic_tag_revision"] == 1
        assert detail["admission"] == original["admission"]
        assert detail["recorded_only"] == original["recorded_only"]
        records = client.get(f"/v1/tagging/{identity}/results", headers=HEADERS).json()[
            "records"
        ]
        assert (
            next(r for r in records if r["task_id"] == "111")["dynamic_tags"] == payload
        )
        selected = client.get(
            f"/v1/tagging/{identity}/selected", headers=HEADERS
        ).json()
        assert (
            next(r for r in selected if r["task_id"] == "111")["dynamic_tags"]
            == payload
        )
        downloaded = client.get(f"/v1/tagging/{identity}/download", headers=HEADERS)
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            assert json.loads(archive.read("tasks/111.json"))["dynamic_tags"] == payload
            for filename in ("index.json", "selected-tasks.json"):
                value = json.loads(archive.read(filename))
                rows = value["records"] if isinstance(value, dict) else value
                assert (
                    next(r for r in rows if r["task_id"] == "111")["dynamic_tags"]
                    == payload
                )
        assert not list((tmp_path / "_tagging" / identity).glob("download-*.zip"))
        # Each replacement is the whole map: omitted tags are not silently merged.
        only_review = {"review": payload["review"]}
        replaced = client.post(
            url,
            headers={**HEADERS, "If-Match": updated.headers["ETag"]},
            json=only_review,
        )
        assert replaced.status_code == 200 and replaced.json() == only_review
    with TestClient(create_app(config)) as client:
        current = client.get(url, headers=HEADERS)
        assert current.json() == only_review
        cleared = client.post(
            url, headers={**HEADERS, "If-Match": current.headers["ETag"]}, json={}
        )
        assert cleared.status_code == 200 and cleared.json() == {}
    with TestClient(create_app(config)) as client:
        assert client.get(url, headers=HEADERS).json() == {}


def test_dynamic_tag_auth_task_isolation_and_readiness(tmp_path):
    seed_export(tmp_path, task_ids=("111", "222"))
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        identity = create_tagging(client)
        url = f"/v1/tagging/{identity}/task/111"
        first = client.get(url, headers=HEADERS)
        assert client.get(url).status_code == 401
        assert client.post(url, json={}).status_code == 401
        assert client.get("/v1/tagging/tag-schema").status_code == 401
        for task_id in ("0", "999", "invalid"):
            missing = f"/v1/tagging/{identity}/task/{task_id}"
            assert client.get(missing, headers=HEADERS).status_code == 404
            assert (
                client.post(
                    missing,
                    headers={**HEADERS, "If-Match": first.headers["ETag"]},
                    json={},
                ).status_code
                == 404
            )
        other = f"/v1/tagging/{identity}/task/222"
        assert (
            client.post(
                other, headers={**HEADERS, "If-Match": first.headers["ETag"]}, json={}
            ).status_code
            == 412
        )
        unknown = f"/v1/tagging/{'b' * 32}/task/111"
        assert client.get(unknown, headers=HEADERS).status_code == 404
        job = app.state.tagging.get(identity)
        app.state.tagging.jobs[identity] = {
            **job,
            "archive_ready": False,
            "status": "running",
        }
        assert client.get(url, headers=HEADERS).status_code == 409
        assert (
            client.post(
                url, headers={**HEADERS, "If-Match": first.headers["ETag"]}, json={}
            ).status_code
            == 409
        )


def test_download_uses_one_tag_snapshot_during_concurrent_update(tmp_path, monkeypatch):
    seed_export(tmp_path)
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        identity = create_tagging(client)
        url = f"/v1/tagging/{identity}/task/111"
        original = client.get(url, headers=HEADERS)
        entered, release = threading.Event(), threading.Event()
        task_file = app.state.tagging._task_file

        def pause_once(*args):
            if not entered.is_set():
                entered.set()
                assert release.wait(5)
            return task_file(*args)

        monkeypatch.setattr(app.state.tagging, "_task_file", pause_once)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                client.get, f"/v1/tagging/{identity}/download", headers=HEADERS
            )
            try:
                assert entered.wait(5)
                changed = client.post(
                    url,
                    headers={**HEADERS, "If-Match": original.headers["ETag"]},
                    json={},
                )
                assert changed.status_code == 200
            finally:
                release.set()
            downloaded = pending.result(timeout=5)
        assert downloaded.status_code == 200
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            task = json.loads(archive.read("tasks/111.json"))
            index = json.loads(archive.read("index.json"))
            selected = json.loads(archive.read("selected-tasks.json"))
            assert task["dynamic_tags"] == original.json()
            assert index["records"][0]["dynamic_tags"] == task["dynamic_tags"]
            assert selected[0]["dynamic_tags"] == task["dynamic_tags"]
        assert client.get(url, headers=HEADERS).json() == {}


def seed_export(root, *, task_ids=("111",), status="completed"):
    directory = root / EXPORT_ID
    directory.mkdir(parents=True)
    for task_id in task_ids:
        task = directory / "tasks" / task_id
        task.mkdir(parents=True)
        report = {
            "schema": "unit_test/report@2",
            "run": {"run_id": f"r-{task_id}", "flux_task_id": task_id},
            "summary": {
                "passed": 2,
                "failed": 1 if task_id == "111" else 0,
                "blocked": 0,
                "not_run": 0,
                "skipped": 0,
            },
        }
        (task / "report.json").write_text(json.dumps(report))
        datasets = {
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
        }
        datasets["artifacts"].update(
            status="available",
            complete=True,
            files=[
                {
                    "path": "report.json",
                    "format": "bytes.v1",
                    "pointer": "",
                    "role": "artifact",
                }
            ],
        )
        if task_id == "111":
            (task / "execution.json").write_text(
                json.dumps(
                    {
                        "schema_version": "trace-read-model.v2",
                        "sessions": [
                            {
                                "id": "session-111",
                                "turns": [
                                    {
                                        "id": "turn-111",
                                        "steps": [
                                            {
                                                "kind": "tool_call",
                                                "title": "Tool Call: bash",
                                                "status": "success",
                                                "payload": {
                                                    "name": "bash",
                                                    "call_id": "call-111",
                                                    "parsed_arguments": {
                                                        "command": "pytest -q"
                                                    },
                                                },
                                                "result": {"output": ""},
                                                "children": [],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                )
            )
            (task / "task.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "task_id": task_id,
                                "task": {
                                    "task_id": task_id,
                                    "repo_path": "",
                                    "commit_list": [{"repo_name": "org/service"}],
                                    "related_logic_tasks": [
                                        {"level_1_category": "测试"}
                                    ],
                                },
                            }
                        ]
                    }
                )
            )
            for name, fmt in [
                ("execution", "trace-read-model.v2"),
                ("task", "json-object.v1"),
            ]:
                datasets[name].update(
                    status="available",
                    complete=True,
                    files=[
                        {
                            "path": f"{name}.json",
                            "format": fmt,
                            "pointer": "",
                            "role": "data",
                        }
                    ],
                )
        (task / "data-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "task_id": task_id,
                    "finalized": True,
                    "task_status": "ok",
                    "datasets": datasets,
                }
            )
        )
    (directory / "job.json").write_text(
        json.dumps(
            {
                "job_id": EXPORT_ID,
                "status": status,
                "archive_ready": True,
                "tasks": {identity: {"status": "ok"} for identity in task_ids},
            }
        )
    )
    with zipfile.ZipFile(directory / "export.zip", "w") as archive:
        archive.writestr("original.txt", "original export")
    return directory


def settings(root, **kwargs):
    return Settings(
        _env_file=None, export_dir=root, api_key=SecretStr("caller-key"), **kwargs
    )


def wait_for_job(client, identity):
    for _ in range(300):
        response = client.get(f"/v1/tagging/{identity}", headers=HEADERS)
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running", "packaging"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Tagging did not finish")


def test_end_to_end_tagging_filter_details_download_and_restart(tmp_path):
    source = seed_export(tmp_path, task_ids=("111", "222"))
    original = (source / "export.zip").read_bytes()
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request))
    config = settings(tmp_path)
    with TestClient(create_app(config, transport=transport)) as client:
        created = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
        )
        assert created.status_code == 202
        identity = created.json()["tagging_id"]
        job = wait_for_job(client, identity)
        assert job["status"] == "completed"
        assert job["decision_counts"] == {"keep": 2}
        page = client.get(
            f"/v1/tagging/{identity}/results?candidate_level=priority_candidate",
            headers=HEADERS,
        ).json()
        assert page["total"] == 1 and page["records"][0]["task_id"] == "111"
        detail = client.get(f"/v1/tagging/{identity}/tasks/111", headers=HEADERS).json()
        assert detail["schema_version"] == "flux.test-admission.v2"
        assert "datasets" not in detail and "policy" not in detail
        assert detail["core"]["problem_signal"] is True
        assert detail["admission"]["evidence_refs"]
        repo = detail["recorded_only"]["repository"]
        assert repo["repo_path"] == ""
        assert repo["extraction"]["repositories"][0]["name"] == "org/service"
        assert repo["extraction"]["repositories"][0]["categories"] == [
            "commit_metadata"
        ]
        assert page["records"][0]["tags"] == detail["recorded_only"]
        assert page["records"][0]["tags"]["platform"]["logic_categories"] == [
            {"level_1_category": "测试"}
        ]
        selected = client.get(f"/v1/tagging/{identity}/selected", headers=HEADERS)
        assert selected.status_code == 200
        assert {record["task_id"] for record in selected.json()} == {"111", "222"}
        assert "input_dir" not in selected.text
        selected_task = next(
            record for record in selected.json() if record["task_id"] == "111"
        )
        assert selected_task["tags"] == detail["recorded_only"]
        downloaded = client.get(f"/v1/tagging/{identity}/download", headers=HEADERS)
        with zipfile.ZipFile(BytesIO(downloaded.content)) as archive:
            index = json.loads(archive.read("index.json"))
            index_task = next(
                record for record in index["records"] if record["task_id"] == "111"
            )
            assert index_task["tags"] == detail["recorded_only"]
            assert set(archive.namelist()) == {
                "index.json",
                "selected-tasks.json",
                "tasks/111.json",
                "tasks/222.json",
            }
        assert (
            client.get(f"/v1/tagging/{identity}/tasks/999", headers=HEADERS).status_code
            == 404
        )
        assert calls == []
    with TestClient(create_app(config)) as client:
        job = client.get(f"/v1/tagging/{identity}", headers=HEADERS).json()
        assert job["archive_ready"] is True
        assert (
            client.get(f"/v1/tagging/{identity}/selected", headers=HEADERS).status_code
            == 200
        )
    assert (source / "export.zip").read_bytes() == original


def test_priority_policy_and_missing_manifest_isolated(tmp_path):
    source = seed_export(tmp_path, task_ids=("111", "222", "333"))
    (source / "tasks/333/data-manifest.json").unlink()
    with TestClient(create_app(settings(tmp_path))) as client:
        created = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging",
            headers=HEADERS,
            json={"policy": {"require_problem_signal": True}},
        )
        identity = created.json()["tagging_id"]
        job = wait_for_job(client, identity)
        assert job["status"] == "partial"
        assert job["decision_counts"] == {"keep": 1, "exclude": 1, "hold": 1}
        selected = client.get(
            f"/v1/tagging/{identity}/selected", headers=HEADERS
        ).json()
        assert [r["task_id"] for r in selected] == ["111"]
        records = client.get(f"/v1/tagging/{identity}/results", headers=HEADERS).json()[
            "records"
        ]
        failed = next(record for record in records if record["task_id"] == "333")
        assert failed["tags"]["data_quality"]["analysis_error_codes"]


def test_authentication_validation_and_unfinished_source(tmp_path):
    seed_export(tmp_path)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert (
            client.post(f"/v1/exports/{EXPORT_ID}/tagging", json={}).status_code == 401
        )
        assert client.get(f"/v1/tagging/{EXPORT_ID}/selected").status_code == 401
        assert (
            client.post(
                f"/v1/exports/{EXPORT_ID}/tagging",
                headers=HEADERS,
                json={"task_dir": "/private/tmp"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/v1/exports/{'b' * 32}/tagging", headers=HEADERS, json={}
            ).status_code
            == 404
        )
        manager = client.app.state.exports
        manager.jobs[EXPORT_ID] = {
            "job_id": EXPORT_ID,
            "status": "running",
            "archive_ready": False,
            "tasks": {"111": {}},
        }
        assert (
            client.post(
                f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
            ).status_code
            == 409
        )
        assert (
            client.get("/v1/tagging/invalid/results", headers=HEADERS).status_code
            == 404
        )


def test_cancellation_waits_for_current_read_and_bounds_concurrency(
    tmp_path, monkeypatch
):
    seed_export(tmp_path, task_ids=("111", "222"))
    started, release = threading.Event(), threading.Event()
    original = tagging_service.analyze_task

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(tagging_service, "analyze_task", slow)
    with TestClient(create_app(settings(tmp_path, max_tagging_jobs=1))) as client:
        created = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
        )
        identity = created.json()["tagging_id"]
        try:
            assert started.wait(2)
            assert (
                client.post(
                    f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
                ).status_code
                == 429
            )
            assert (
                client.get(
                    f"/v1/tagging/{identity}/selected", headers=HEADERS
                ).status_code
                == 409
            )
            assert (
                client.post(
                    f"/v1/tagging/{identity}/cancel", headers=HEADERS
                ).status_code
                == 200
            )
            assert (
                client.get(f"/v1/tagging/{identity}", headers=HEADERS).json()["status"]
                == "running"
            )
        finally:
            release.set()
        job = wait_for_job(client, identity)
        assert job["status"] == "cancelled"
        assert job["completed_count"] == 1
        assert job["decision_counts"] == {"keep": 1, "hold": 1}


def test_persisted_running_job_is_reported_interrupted(tmp_path):
    directory = tmp_path / "_tagging" / EXPORT_ID
    directory.mkdir(parents=True)
    (directory / "job.json").write_text(
        json.dumps(
            {"tagging_id": EXPORT_ID, "status": "running", "archive_ready": False}
        )
    )
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.get(f"/v1/tagging/{EXPORT_ID}", headers=HEADERS)
        assert response.json()["status"] == "interrupted"


def test_tagging_source_cannot_escape_export_directory(tmp_path):
    source = seed_export(tmp_path)
    outside = tmp_path.parent / "outside-task"
    outside.mkdir(exist_ok=True)
    task = source / "tasks/111"
    # Leave the valid job index but replace its task directory with a symlink.
    import shutil

    shutil.rmtree(task)
    task.symlink_to(outside, target_is_directory=True)
    with TestClient(create_app(settings(tmp_path))) as client:
        identity = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
        ).json()["tagging_id"]
        job = wait_for_job(client, identity)
        assert job["decision_counts"] == {"hold": 1}
        result = client.get(f"/v1/tagging/{identity}/tasks/111", headers=HEADERS).json()
        assert result["admission"]["reason_codes"] == ["INVALID_PATH"]


def test_malformed_business_payload_does_not_stop_other_tasks(tmp_path, monkeypatch):
    seed_export(tmp_path, task_ids=("111", "222"))
    original = tagging_service.analyze_task

    def malformed(path, **kwargs):
        if path.name == "111":
            raise TypeError("Malformed internal source field")
        return original(path, **kwargs)

    monkeypatch.setattr(tagging_service, "analyze_task", malformed)
    with TestClient(create_app(settings(tmp_path))) as client:
        identity = client.post(
            f"/v1/exports/{EXPORT_ID}/tagging", headers=HEADERS, json={}
        ).json()["tagging_id"]
        job = wait_for_job(client, identity)
        assert job["status"] == "partial"
        assert job["decision_counts"] == {"hold": 1, "keep": 1}
        result = client.get(f"/v1/tagging/{identity}/tasks/111", headers=HEADERS).json()
        assert result["admission"]["reason_codes"] == ["TASK_ANALYSIS_FAILED"]
