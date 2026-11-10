"""Subset downloads retain task bytes and contracts from one published ZIP."""

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service import export_download
from flux_task_service.app import create_app
from flux_task_service.config import Settings
from flux_task_service.export_contract import DATASETS, TaskDataReader

JOB = "a" * 32
URL = f"/v1/exports/{JOB}/download"
HEADERS = {"Authorization": "Bearer test-key"}


def seed(root, marker="original", filename="export.zip"):
    directory = root / JOB
    directory.mkdir(exist_ok=True)
    job = {
        "schema_version": "1.0",
        "job_id": JOB,
        "status": "partial",
        "archive_ready": True,
        "task_count": 2,
        "completed_count": 2,
        "tasks": {
            task_id: {"status": "ok", "components": {}} for task_id in ("111", "222")
        },
        "request": {"task_ids": ["111", "222"], "filters": None},
        "task_requests": {
            task_id: {"task_ids": [task_id]} for task_id in ("111", "222")
        },
        "retry_request": {"task_ids": ["222"]},
    }
    (directory / "job.json").write_text(json.dumps(job))
    with zipfile.ZipFile(directory / filename, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("job.json", json.dumps(job))
        archive.writestr("task_ids.json", '["111", "222"]')
        archive.writestr(
            "selection.json",
            json.dumps(
                {
                    "task_ids": ["111", "222"],
                    "count": 2,
                    "scanned_count": 10,
                }
            ),
        )
        for task_id in ("111", "222"):
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
            datasets["task"].update(
                status="available",
                complete=True,
                files=[
                    {
                        "path": "task.json",
                        "format": "json-object.v1",
                        "pointer": "",
                        "role": "data",
                    }
                ],
            )
            manifest = {
                "schema_version": "1.0",
                "task_id": task_id,
                "finalized": True,
                "task_status": "ok",
                "datasets": datasets,
            }
            archive.writestr(
                f"tasks/{task_id}/data-manifest.json", json.dumps(manifest)
            )
            archive.writestr(
                f"tasks/{task_id}/task.json", json.dumps({"marker": marker})
            )
            archive.writestr(
                f"tasks/{task_id}/trace/openapi.json", json.dumps({"marker": marker})
            )
            archive.writestr(
                f"tasks/{task_id}/artifacts/export.zip", b"\x00\x01artifact"
            )
    return directory


def app(root, **kwargs):
    return create_app(
        Settings(
            _env_file=None, export_dir=root, api_key=SecretStr("test-key"), **kwargs
        )
    )


def test_single_and_batch_download_keep_files_and_narrow_metadata(tmp_path):
    directory = seed(tmp_path)
    original = (directory / "export.zip").read_bytes()
    with TestClient(app(tmp_path)) as client:
        single = client.get(URL, headers=HEADERS, params={"task_id": "111"})
        assert single.status_code == 200
        with zipfile.ZipFile(io.BytesIO(single.content)) as archive:
            assert not any(name.startswith("tasks/222/") for name in archive.namelist())
            job = json.loads(archive.read("job.json"))
            assert list(job["tasks"]) == ["111"]
            assert job["task_count"] == job["completed_count"] == 1
            assert job["request"]["task_ids"] == ["111"]
            assert job["task_requests"] == {"111": {"task_ids": ["111"]}}
            assert "retry_request" not in job
            assert json.loads(archive.read("task_ids.json")) == ["111"]
            selection = json.loads(archive.read("selection.json"))
            assert selection == {
                "task_ids": ["111"],
                "count": 1,
                "source_count": 2,
                "scanned_count": 10,
            }
            assert archive.read("tasks/111/artifacts/export.zip") == b"\x00\x01artifact"
            assert job["bytes_written"] == sum(
                info.file_size
                for info in archive.infolist()
                if info.filename != "job.json"
            )
            archive.extractall(tmp_path / "unpacked")
        reader = TaskDataReader(tmp_path / "unpacked/tasks/111")
        assert reader.read_file(reader.manifest.datasets["task"].files[0]) == {
            "marker": "original"
        }
        multiple = client.get(
            URL,
            headers=HEADERS,
            params=[
                ("task_id", "222"),
                ("task_id", "111"),
                ("task_id", "222"),
            ],
        )
        with zipfile.ZipFile(io.BytesIO(multiple.content)) as archive:
            assert json.loads(archive.read("task_ids.json")) == ["111", "222"]
            assert len(archive.namelist()) == len(set(archive.namelist()))
        assert client.get(URL, headers=HEADERS).content == original
        assert (directory / "export.zip").read_bytes() == original


def test_validation_auth_readiness_and_corrupt_archive(tmp_path):
    directory = seed(tmp_path)
    with TestClient(app(tmp_path, max_task_ids=2)) as client:
        assert client.get(URL, params={"task_id": "111"}).status_code == 401
        for invalid in ("", "0", "abc", "../111", "111,222", "1" * 65):
            assert (
                client.get(
                    URL, headers=HEADERS, params={"task_id": invalid}
                ).status_code
                == 422
            )
        assert (
            client.get(URL, headers=HEADERS, params={"task_id": "999"}).status_code
            == 404
        )
        assert (
            client.get(
                URL, headers=HEADERS, params=[("task_id", "111")] * 3
            ).status_code
            == 422
        )
        job = json.loads((directory / "job.json").read_text())
        (directory / "job.json").write_text(json.dumps({**job, "archive_ready": False}))
        assert (
            client.get(URL, headers=HEADERS, params={"task_id": "111"}).status_code
            == 409
        )
        (directory / "job.json").write_text(json.dumps(job))
        (directory / "export.zip").write_bytes(b"corrupt")
        assert (
            client.get(URL, headers=HEADERS, params={"task_id": "111"}).status_code
            == 503
        )


def test_snapshot_and_temporary_cleanup(tmp_path, monkeypatch):
    directory = seed(tmp_path)
    seed(tmp_path, "new", "replacement.zip")
    temporary_paths = []
    temporary_file = export_download.tempfile.NamedTemporaryFile

    def track_file(**kwargs):
        file = temporary_file(dir=tmp_path, **kwargs)
        temporary_paths.append(Path(file.name))
        return file

    monkeypatch.setattr(export_download.tempfile, "NamedTemporaryFile", track_file)
    copy = export_download.shutil.copyfileobj
    replaced = False

    def replace_during_copy(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            (directory / "replacement.zip").replace(directory / "export.zip")
            replaced = True
        return copy(*args, **kwargs)

    monkeypatch.setattr(export_download.shutil, "copyfileobj", replace_during_copy)
    with TestClient(app(tmp_path)) as client:
        response = client.get(URL, headers=HEADERS, params={"task_id": "111"})
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert json.loads(archive.read("tasks/111/task.json")) == {
                "marker": "original"
            }
            assert json.loads(archive.read("tasks/111/trace/openapi.json")) == {
                "marker": "original"
            }
        assert temporary_paths and all(not path.exists() for path in temporary_paths)

        def fail_copy(*args, **kwargs):
            raise OSError("simulated write failure")

        monkeypatch.setattr(export_download.shutil, "copyfileobj", fail_copy)
        assert (
            client.get(URL, headers=HEADERS, params={"task_id": "111"}).status_code
            == 503
        )
        assert all(not path.exists() for path in temporary_paths)
