"""Bounded background tagging of server-owned, completed export snapshots."""

import asyncio
import json
import re
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from flux_task_service.config import Settings
from flux_task_service.errors import ServiceError
from flux_task_service.export_contract import ContractError, read_json
from flux_task_service.exports import ExportManager

from .__main__ import _write
from .analyzer import analyze_task
from .models import TaggingPolicy
from .presentation import compact_result
from .tag_schema import initial_tags
from .tag_store import TagSnapshot, read_tags, replace_tags

TERMINAL_EXPORTS = {"completed", "partial", "failed", "cancelled", "interrupted"}


class TaggingQuery(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    policy: TaggingPolicy = Field(default_factory=TaggingPolicy)
    allow_legacy: bool = False


class TaggingManager:
    def __init__(self, exports: ExportManager, settings: Settings):
        self.exports = exports
        self.settings = settings
        self.jobs: dict[str, dict] = {}
        self.running: dict[str, asyncio.Task] = {}

    def directory(self, tagging_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", tagging_id):
            raise ServiceError("TAGGING_NOT_FOUND", "Tagging job not found", 404)
        root = self.settings.export_dir.resolve() / "_tagging"
        path = root / tagging_id
        if not path.resolve().is_relative_to(self.settings.export_dir.resolve()):
            raise ServiceError("TAGGING_NOT_FOUND", "Tagging job not found", 404)
        return path

    def save(self, job: dict) -> None:
        _write(self.directory(job["tagging_id"]) / "job.json", job)

    def get(self, tagging_id: str) -> dict:
        path = self.directory(tagging_id)
        if tagging_id in self.jobs:
            return self.jobs[tagging_id]
        try:
            job = read_json(path / "job.json")
        except ContractError as exc:
            raise ServiceError(
                "TAGGING_NOT_FOUND", "Tagging job not found", 404
            ) from exc
        if not isinstance(job, dict) or job.get("tagging_id") != tagging_id:
            raise ServiceError("TAGGING_NOT_FOUND", "Tagging job not found", 404)
        if job["status"] in {"queued", "running", "packaging"}:
            job = {**job, "status": "interrupted", "archive_ready": False}
        return job

    def create(self, export_id: str, query: TaggingQuery) -> dict:
        export = self.exports.get(export_id)
        if export.get("status") not in TERMINAL_EXPORTS or not export.get(
            "archive_ready"
        ):
            raise ServiceError("EXPORT_NOT_READY", "Wait for the export to finish", 409)
        source = self.exports.directory(export_id)
        if not source.resolve().is_relative_to(self.settings.export_dir.resolve()):
            raise ServiceError("EXPORT_NOT_FOUND", "Export job not found", 404)
        task_ids = list(export.get("tasks", {}))
        if not task_ids:
            raise ServiceError("TAGGING_NO_TASKS", "Export contains no tasks", 422)
        if any(not re.fullmatch(r"[1-9][0-9]{0,63}", task_id) for task_id in task_ids):
            raise ServiceError(
                "INVALID_EXPORT_INDEX", "Export task index is invalid", 422
            )
        if len(task_ids) > self.settings.max_task_ids:
            raise ServiceError("QUERY_LIMIT_EXCEEDED", "Too many tasks to tag", 422)
        if len(self.running) >= self.settings.max_tagging_jobs:
            raise ServiceError("TAGGING_BUSY", "Too many active tagging jobs", 429)
        tagging_id = uuid.uuid4().hex
        job = {
            "schema_version": "flux.tagging-job.v1",
            "tagging_id": tagging_id,
            "export_job_id": export_id,
            "status": "queued",
            "created_at": int(time.time() * 1000),
            "completed_count": 0,
            "task_count": len(task_ids),
            "decision_counts": {},
            "error_count": 0,
            "policy": query.policy.model_dump(),
            "allow_legacy": query.allow_legacy,
            "archive_ready": False,
            "error": None,
        }
        try:
            self.save(job)
        except OSError as exc:
            raise ServiceError(
                "TAGGING_WRITE_FAILED", "Cannot store tagging job", 503
            ) from exc
        self.jobs[tagging_id] = job
        self.running[tagging_id] = asyncio.create_task(self.run(job, query, task_ids))
        return dict(job)

    async def run(self, job: dict, query: TaggingQuery, task_ids: list[str]) -> None:
        records = []
        output = self.directory(job["tagging_id"])
        source = self.exports.directory(job["export_job_id"]).resolve()
        try:
            job["status"] = "running"
            self.save(job)
            for task_id in task_ids:
                if job.get("cancel_requested"):
                    break
                path = source / "tasks" / task_id
                try:
                    if not path.resolve().is_relative_to(source):
                        raise ContractError(
                            "INVALID_PATH", "Task directory is outside export"
                        )
                    operation = asyncio.create_task(
                        asyncio.to_thread(
                            analyze_task,
                            path,
                            policy=query.policy,
                            allow_legacy=query.allow_legacy,
                        )
                    )
                    try:
                        result = await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        # Threads cannot be forcibly stopped. Finish the current read
                        # before releasing the slot or advertising stopped state.
                        job["cancel_requested"] = True
                        result = await operation
                except Exception as exc:
                    job["error_count"] += 1
                    code = (
                        exc.code
                        if isinstance(exc, ContractError)
                        else "TASK_ANALYSIS_FAILED"
                    )
                    message = (
                        str(exc)
                        if isinstance(exc, ContractError)
                        else "Cannot analyze task payload"
                    )
                    result = {
                        "task_id": task_id,
                        "admission": {
                            "candidate_level": "review",
                            "decision": "hold",
                            "reason_codes": [code],
                        },
                        "analysis_errors": [{"code": code, "message": message}],
                    }
                if result.get("schema_version") != "flux.test-admission.v2":
                    result = compact_result(result)
                _write(output / "tasks" / f"{task_id}.json", result)
                records.append(
                    {
                        "task_id": task_id,
                        "export_job_id": job["export_job_id"],
                        "task_path": f"tasks/{task_id}",
                        "result": f"tasks/{task_id}.json",
                        **result["admission"],
                        "tags": result["recorded_only"],
                        "test_evidence_level": result.get("core", {}).get(
                            "test_evidence_level"
                        ),
                        "test_types": result.get("core", {}).get("test_types", []),
                    }
                )
                job["completed_count"] = len(records)
                job["decision_counts"] = dict(Counter(r["decision"] for r in records))
                self.save(job)
            for task_id in task_ids[len(records) :]:
                result = {
                    "task_id": task_id,
                    "admission": {
                        "candidate_level": "review",
                        "decision": "hold",
                        "reason_codes": ["TAGGING_CANCELLED"],
                    },
                }
                if result.get("schema_version") != "flux.test-admission.v2":
                    result = compact_result(result)
                _write(output / "tasks" / f"{task_id}.json", result)
                records.append(
                    {
                        "task_id": task_id,
                        "export_job_id": job["export_job_id"],
                        "task_path": f"tasks/{task_id}",
                        "result": f"tasks/{task_id}.json",
                        **result["admission"],
                        "tags": result["recorded_only"],
                        "test_evidence_level": "unknown",
                        "test_types": [],
                    }
                )
            index = {
                "schema_version": "flux.test-admission-index.v2",
                "export_job_id": job["export_job_id"],
                "tagging_id": job["tagging_id"],
                "policy": job["policy"],
                "records": records,
                "task_count": len(records),
                "decision_counts": dict(Counter(r["decision"] for r in records)),
            }
            _write(output / "index.json", index)
            _write(
                output / "selected-tasks.json",
                [r for r in records if r["decision"] == "keep"],
            )
            job["decision_counts"] = index["decision_counts"]
            job["status"] = "packaging"
            self.save(job)
            package = asyncio.create_task(asyncio.to_thread(self.package, output))
            try:
                await asyncio.shield(package)
            except asyncio.CancelledError:
                job["cancel_requested"] = True
                await package
            job["archive_ready"] = True
            job["status"] = (
                "cancelled"
                if job.get("cancel_requested")
                else "partial"
                if job["error_count"]
                else "completed"
            )
        except Exception:
            job["status"] = "failed"
            job["error"] = {
                "code": "TAGGING_FAILED",
                "message": "Tagging or result storage failed",
            }
        finally:
            job["finished_at"] = int(time.time() * 1000)
            saved = True
            try:
                self.save(job)
            except OSError:
                saved = False
                job["archive_ready"] = False
                job["status"] = "failed"
                job["error"] = {
                    "code": "TAGGING_WRITE_FAILED",
                    "message": "Cannot store final status",
                }
            self.running.pop(job["tagging_id"], None)
            if saved:
                self.jobs.pop(job["tagging_id"], None)

    @staticmethod
    def package(output: Path) -> None:
        temporary = output / "results.zip.tmp"
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in [
                output / "index.json",
                output / "selected-tasks.json",
                *sorted((output / "tasks").glob("*.json")),
            ]:
                archive.write(path, path.relative_to(output).as_posix())
        temporary.replace(output / "results.zip")

    def _base_results(self, tagging_id: str) -> dict:
        job = self.get(tagging_id)
        if not job.get("archive_ready"):
            raise ServiceError(
                "TAGGING_NOT_READY", "Tagging results are not ready", 409
            )
        try:
            return read_json(self.directory(tagging_id) / "index.json")
        except ContractError as exc:
            raise ServiceError(
                "TAGGING_READ_FAILED", "Cannot read tagging results", 503
            ) from exc

    def _task_file(self, tagging_id: str, task_id: str) -> dict:
        if not re.fullmatch(r"[1-9][0-9]{0,63}", task_id):
            raise ServiceError("TAGGING_TASK_NOT_FOUND", "Task result not found", 404)
        try:
            return read_json(self.directory(tagging_id) / "tasks" / f"{task_id}.json")
        except ContractError as exc:
            raise ServiceError(
                "TAGGING_TASK_NOT_FOUND", "Task result not found", 404
            ) from exc

    @staticmethod
    def _tag_snapshot(task_id: str, recorded: dict, edits: dict) -> TagSnapshot:
        return (
            edits[task_id] if task_id in edits else TagSnapshot(initial_tags(recorded))
        )

    def results(self, tagging_id: str) -> dict:
        index = self._base_results(tagging_id)
        edits = read_tags(self.directory(tagging_id))
        for record in index["records"]:
            snapshot = self._tag_snapshot(
                record["task_id"], record.get("tags", {}), edits
            )
            record.update(snapshot.fields())
        return index

    def _base_task_result(self, tagging_id: str, task_id: str) -> dict:
        index = self._base_results(tagging_id)
        if not any(record["task_id"] == task_id for record in index["records"]):
            raise ServiceError("TAGGING_TASK_NOT_FOUND", "Task result not found", 404)
        return self._task_file(tagging_id, task_id)

    def task_result(self, tagging_id: str, task_id: str) -> dict:
        result = self._base_task_result(tagging_id, task_id)
        snapshot = self._tag_snapshot(
            task_id,
            result.get("recorded_only", {}),
            read_tags(self.directory(tagging_id)),
        )
        result.update(snapshot.fields())
        return result

    def editable_tags(self, tagging_id: str, task_id: str) -> TagSnapshot:
        result = self.task_result(tagging_id, task_id)
        return TagSnapshot(
            result["dynamic_tags"],
            result["dynamic_tag_revision"],
            result["dynamic_tag_updated_at"],
        )

    def update_tags(
        self,
        tagging_id: str,
        task_id: str,
        tags: dict,
        expected_etag: str | None,
    ) -> TagSnapshot:
        result = self._base_task_result(tagging_id, task_id)
        return replace_tags(
            self.directory(tagging_id),
            tagging_id,
            task_id,
            tags,
            expected_etag,
            initial_tags(result.get("recorded_only", {})),
        )

    def download(self, tagging_id: str, task_ids: list[str] | None = None) -> Path:
        """Build all archive views using one committed tag snapshot."""
        index = self._base_results(tagging_id)
        if task_ids is not None:
            if not task_ids or any(
                not re.fullmatch(r"[1-9][0-9]{0,63}", task_id) for task_id in task_ids
            ):
                raise ServiceError(
                    "INVALID_TASK_IDS", "task_id must be a nonempty numeric ID", 422
                )
            if len(task_ids) > self.settings.max_task_ids:
                raise ServiceError(
                    "QUERY_LIMIT_EXCEEDED", "Too many tasks to download", 422
                )
            selected = set(task_ids)
            available = {record["task_id"] for record in index["records"]}
            if not selected <= available:
                raise ServiceError(
                    "TAGGING_TASK_NOT_FOUND",
                    "One or more tasks do not belong to this tagging job",
                    404,
                )
            index["records"] = [
                record for record in index["records"] if record["task_id"] in selected
            ]
            index["task_count"] = len(index["records"])
            index["decision_counts"] = dict(
                Counter(record["decision"] for record in index["records"])
            )
        directory = self.directory(tagging_id)
        edits = read_tags(directory)
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="download-", suffix=".zip", dir=directory, delete=False
            ) as stream:
                path = Path(stream.name)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for record in index["records"]:
                    task_id = record["task_id"]
                    result = self._task_file(tagging_id, task_id)
                    snapshot = self._tag_snapshot(
                        task_id, record.get("tags", {}), edits
                    )
                    result.update(snapshot.fields())
                    record.update(snapshot.fields())
                    archive.writestr(
                        f"tasks/{task_id}.json",
                        json.dumps(
                            result, ensure_ascii=False, indent=2, allow_nan=False
                        ),
                    )
                for name, value in (
                    ("index.json", index),
                    (
                        "selected-tasks.json",
                        [r for r in index["records"] if r["decision"] == "keep"],
                    ),
                ):
                    archive.writestr(
                        name,
                        json.dumps(
                            value, ensure_ascii=False, indent=2, allow_nan=False
                        ),
                    )
            return path
        except Exception:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def cancel(self, tagging_id: str) -> dict:
        job = self.get(tagging_id)
        if tagging_id in self.running and job["status"] in {"queued", "running"}:
            job["cancel_requested"] = True
            self.save(job)
        return dict(job)

    async def close(self) -> None:
        for tagging_id in list(self.running):
            self.cancel(tagging_id)
        await asyncio.gather(*list(self.running.values()), return_exceptions=True)
