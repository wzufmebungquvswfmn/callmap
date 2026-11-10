"""Bounded background export jobs with per-component results and ZIP output."""

import asyncio
import base64
import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from flux_task_service.artifact_downloads import ArtifactDownloads
from flux_task_service.config import Settings
from flux_task_service.conversation import fetch_conversation
from flux_task_service.errors import ServiceError
from flux_task_service.export_contract import (
    CONTRACT_ERROR_CODES,
    VERSION,
    build_manifest,
)
from flux_task_service.export_download import subset_archive
from flux_task_service.models import (
    CollectQuery,
    ExportQuery,
    ExportRetryQuery,
    TaskIdentity,
    TraceQuery,
)
from flux_task_service.resources import (
    ResourceService,
    public_error,
    safe_artifact_path,
)
from flux_task_service.service import TaskService


def encode_json(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()


class ExportManager:
    def __init__(
        self,
        resources: ResourceService,
        tasks: TaskService,
        settings: Settings,
        admission: asyncio.Semaphore,
    ):
        self.resources = resources
        self.tasks = tasks
        self.settings = settings
        self.admission = admission
        self.jobs: dict[str, dict] = {}
        self.running: dict[str, asyncio.Task] = {}

    def directory(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
            raise ServiceError("EXPORT_NOT_FOUND", "Export job not found", 404)
        return self.settings.export_dir / job_id

    def save(self, job: dict) -> None:
        root = self.directory(job["job_id"])
        temporary = root / "job.json.tmp"
        temporary.write_bytes(encode_json(job))
        temporary.replace(root / "job.json")

    def get(self, job_id: str) -> dict:
        root = self.directory(job_id)
        if job_id in self.jobs:
            return self.jobs[job_id]
        try:
            job = json.loads((root / "job.json").read_bytes())
        except (OSError, ValueError) as exc:
            raise ServiceError("EXPORT_NOT_FOUND", "Export job not found", 404) from exc
        if job["status"] in {"queued", "running", "packaging"}:
            job["status"] = "interrupted"
            job["archive_ready"] = (
                bool(job.get("archive_ready")) and (root / "export.zip").is_file()
            )
        return job

    async def get_async(self, job_id: str) -> dict:
        """Read a terminal job without blocking the event loop."""
        return await asyncio.to_thread(self.get, job_id)

    def download(self, job_id: str, task_ids: list[str] | None = None) -> Path:
        job = self.get(job_id)
        if not job.get("archive_ready"):
            raise ServiceError("EXPORT_NOT_READY", "Export archive is not ready", 409)
        source = self.directory(job_id) / "export.zip"
        if task_ids is None:
            return source
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
        return subset_archive(source, task_ids)

    def page(
        self,
        job: dict,
        *,
        include_tasks: bool = True,
        task_id: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict:
        """Return a shallow job copy with an optional slice of task records."""
        tasks = job.get("tasks")
        if not isinstance(tasks, dict):
            tasks = {}
        result = dict(job)
        result["tasks_total"] = len(tasks)
        if task_id is not None:
            if task_id not in tasks:
                raise ServiceError(
                    "EXPORT_TASK_NOT_FOUND", "Task is not in the export", 404
                )
            result["tasks"] = {task_id: tasks[task_id]}
            result["tasks_offset"] = 0
            result["tasks_limit"] = None
            result["has_more_tasks"] = False
            return result
        if not include_tasks:
            result.pop("tasks", None)
            result["tasks_offset"] = 0
            result["tasks_limit"] = None
            result["has_more_tasks"] = False
            return result
        items = list(tasks.items())
        total = len(items)
        start = min(offset, total)
        if limit is None:
            selected = items[start:]
            result["tasks_limit"] = None
        else:
            selected = items[start : start + limit]
            result["tasks_limit"] = limit
        result["tasks"] = dict(selected)
        result["tasks_offset"] = start
        result["has_more_tasks"] = start + len(selected) < total
        return result

    def create(
        self,
        query: ExportQuery,
        *,
        retry_of: dict | None = None,
        warnings: list[str] | None = None,
    ) -> dict:
        if len(self.running) >= self.settings.max_export_jobs:
            raise ServiceError("EXPORT_BUSY", "Too many active export jobs", 429)
        if len(query.task_ids) > self.settings.max_task_ids:
            raise ServiceError("QUERY_LIMIT_EXCEEDED", "Too many export task IDs", 422)
        job_id = uuid.uuid4().hex
        job = {
            "schema_version": VERSION,
            "job_id": job_id,
            "status": "queued",
            "created_at": int(time.time() * 1000),
            "completed_count": 0,
            "task_count": len(query.task_ids),
            "components": query.components,
            "tasks": {},
            "bytes_written": 0,
            "archive_ready": False,
            "error": None,
            "request": query.model_dump(mode="json"),
        }
        if retry_of is not None:
            job["retry_of"] = retry_of
        if warnings:
            job["warnings"] = warnings
        try:
            self.directory(job_id).mkdir(parents=True)
            self.save(job)
        except OSError as exc:
            raise ServiceError(
                "EXPORT_WRITE_FAILED", "Cannot create export directory", 503
            ) from exc
        self.jobs[job_id] = job
        self.running[job_id] = asyncio.create_task(self.run(job, query))
        return job

    def retry(
        self, job_id: str, overrides: ExportRetryQuery, *, task_id: str | None = None
    ) -> dict:
        return self._retry(job_id, self.get(job_id), overrides, task_id=task_id)

    async def retry_async(
        self, job_id: str, overrides: ExportRetryQuery, *, task_id: str | None = None
    ) -> dict:
        source = await self.get_async(job_id)
        return self._retry(job_id, source, overrides, task_id=task_id)

    def _retry(
        self,
        job_id: str,
        source: dict,
        overrides: ExportRetryQuery,
        *,
        task_id: str | None = None,
    ) -> dict:
        """Refresh selected tasks in place, keeping the batch identity and scope."""
        if job_id in self.running or source["status"] in {
            "queued",
            "running",
            "packaging",
        }:
            active = source.get("retry_of", {})
            if (
                active.get("job_id") == job_id
                and active.get("task_id") == task_id
                and source.get("retry_overrides")
                == overrides.model_dump(mode="json", exclude_unset=True)
            ):
                return source
            raise ServiceError(
                "EXPORT_STILL_RUNNING", "Wait for the active export to stop", 409
            )
        batch_saved = source.get("request")
        saved = batch_saved
        if task_id is not None:
            saved = source.get("task_requests", {}).get(task_id, saved)
        if saved is not None and not isinstance(saved, dict):
            raise ServiceError(
                "EXPORT_RETRY_INVALID_SOURCE", "Invalid saved export request", 409
            )
        scope_file = self.directory(job_id) / "task_ids.json"
        try:
            if scope_file.exists():
                ids = json.loads(scope_file.read_bytes())
            elif batch_saved and batch_saved.get("task_ids"):
                ids = batch_saved["task_ids"]
            else:
                ids = list(source.get("tasks", {}))
                if len(ids) != source.get("task_count"):
                    raise ServiceError(
                        "EXPORT_RETRY_SCOPE_UNAVAILABLE",
                        "The original task selection is not fully available",
                        409,
                    )
        except (OSError, ValueError, TypeError) as exc:
            raise ServiceError(
                "EXPORT_RETRY_INVALID_SOURCE",
                "Cannot read original task selection",
                409,
            ) from exc
        if not isinstance(ids, list) or any(
            not isinstance(value, str) for value in ids
        ):
            raise ServiceError(
                "EXPORT_RETRY_INVALID_SOURCE", "Invalid original task IDs", 409
            )
        if not ids:
            raise ServiceError(
                "EXPORT_RETRY_EMPTY",
                "No task IDs were selected in the source export",
                409,
            )
        if len(set(ids)) != source.get("task_count"):
            raise ServiceError(
                "EXPORT_RETRY_SCOPE_UNAVAILABLE",
                "Original task count does not match the saved selection",
                409,
            )
        if task_id is not None and task_id not in ids:
            raise ServiceError(
                "EXPORT_TASK_NOT_FOUND", "Task is not in the source export", 404
            )
        options = {
            key: value
            for key, value in (saved or {}).items()
            if key
            in {
                "components",
                "conversation_mode",
                "include_raw_spans",
                "recover_raw_spans",
                "start_ms",
                "end_ms",
            }
        }
        if "components" not in options:
            options["components"] = source.get("components")
        options.update(
            {
                key: value
                for key, value in overrides.model_dump(exclude_unset=True).items()
                if value is not None or key in {"start_ms", "end_ms"}
            }
        )
        try:
            query = ExportQuery(
                task_ids=[task_id] if task_id is not None else ids, **options
            )
            batch_request = batch_saved or ExportQuery(
                task_ids=ids, components=source.get("components")
            ).model_dump(mode="json")
        except ValidationError as exc:
            raise ServiceError(
                "INVALID_QUERY",
                "Retry parameters or original task selection are invalid",
                422,
            ) from exc
        warnings = []
        if saved is None:
            warnings.append(
                "原批次未保存完整请求参数；沿用任务 ID 和组件，"
                "未显式覆盖的参数使用当前默认值（完整会话、不请求 raw、"
                "不启用 raw 恢复、Fornax 使用当前默认时间窗口）。"
            )
        if len(query.task_ids) > self.settings.max_task_ids:
            raise ServiceError("QUERY_LIMIT_EXCEEDED", "Too many export task IDs", 422)
        if len(self.running) >= self.settings.max_export_jobs:
            raise ServiceError("EXPORT_BUSY", "Too many active export jobs", 429)
        ids = list(dict.fromkeys(ids))
        request = query.model_dump(mode="json")
        batch_request = {
            **batch_request,
            "task_ids": ids,
            "filters": None,
        }
        task_requests = dict(source.get("task_requests", {}))
        if task_id is None:
            batch_request = {**request, "task_ids": ids}
            task_requests = {}
        else:
            task_requests[task_id] = request
        records = {
            tid: source.get("tasks", {}).get(
                tid, {"status": "not_started", "components": {}}
            )
            for tid in ids
        }
        for tid in query.task_ids:
            records[tid] = {"status": "queued", "components": {}}
        job = {
            **source,
            "schema_version": VERSION,
            "status": "queued",
            "archive_ready": False,
            "error": None,
            "cancel_requested": False,
            "finished_at": None,
            "bytes_written": source.get("bytes_written", 0),
            "updated_at": int(time.time() * 1000),
            "completed_count": sum(
                record["status"] in {"ok", "partial", "error", "cancelled"}
                for record in records.values()
            ),
            "tasks": records,
            "components": list(
                dict.fromkeys(
                    batch_request["components"]
                    + [c for q in task_requests.values() for c in q["components"]]
                )
            ),
            "request": batch_request,
            "task_requests": task_requests,
            "retry_request": request,
            "retry_overrides": overrides.model_dump(mode="json", exclude_unset=True),
            "warnings": warnings,
            "retry_of": {
                "job_id": job_id,
                "scope": "task" if task_id is not None else "batch",
                "task_id": task_id,
                "parameters": "saved_request"
                if saved is not None
                else "legacy_defaults",
            },
        }
        try:
            self.save(job)
        except OSError as exc:
            raise ServiceError(
                "EXPORT_WRITE_FAILED", "Cannot save export retry", 503
            ) from exc
        self.jobs[job_id] = job
        self.running[job_id] = asyncio.create_task(self.run(job, query, refresh=True))
        return job

    def write(self, job: dict, relative: str, content: bytes) -> None:
        safe_artifact_path(relative)
        path = self.directory(job["job_id"]) / relative
        previous_size = path.stat().st_size if path.exists() else 0
        new_size = job["bytes_written"] - previous_size + len(content)
        if new_size > self.settings.max_export_bytes:
            raise ServiceError(
                "EXPORT_LIMIT_EXCEEDED", "Export exceeded the byte limit", 422
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        job["bytes_written"] = new_size

    def save_task_manifest(
        self, job: dict, task_id: str, query: ExportQuery, *, finalized: bool
    ) -> None:
        record = job["tasks"][task_id]
        relative = f"tasks/{task_id}/data-manifest.json"
        root = self.directory(job["job_id"]) / "tasks" / task_id
        manifest = build_manifest(
            root,
            task_id,
            query.components,
            record["components"],
            finalized=finalized,
            task_status=record["status"],
            raw_requested=query.include_raw_spans,
        )
        contract_errors = [
            {"dataset": name, **error.model_dump()}
            for name, entry in manifest.datasets.items()
            for error in entry.errors
            if error.code in CONTRACT_ERROR_CODES
        ]
        if contract_errors:
            record["contract_errors"] = contract_errors
            if record["status"] == "ok":
                record["status"] = "partial"
                manifest.task_status = "partial"
        self.write(job, relative, encode_json(manifest.model_dump()))
        record["data_manifest_file"] = relative

    async def capture(
        self, job: dict, task_id: str, name: str, operation, results: dict
    ):
        progress = {"stage": name.split("/")[0]}
        job["tasks"][task_id]["progress"] = progress
        try:
            data = await operation()
            relative = f"tasks/{task_id}/{name}.json"
            self.write(job, relative, encode_json(data))
            results[name] = {
                "status": "ok",
                "source": "flux_insight_openapi",
                "file": relative,
            }
            return data
        except (ServiceError, ValidationError, OSError) as exc:
            error = (
                public_error(exc)
                if isinstance(exc, ServiceError)
                else {
                    "code": "EXPORT_COMPONENT_FAILED",
                    "message": "Invalid identity or write failed",
                }
            )
            results[name] = {"status": "error", "error": error}
            return None
        finally:
            if job["tasks"][task_id].get("progress") is progress:
                job["tasks"][task_id].pop("progress", None)

    async def conversation(
        self,
        job: dict,
        task_id: str,
        results: dict,
        data: dict | None = None,
        mode: str = "full",
    ) -> dict:
        if data is None:
            job["tasks"][task_id]["progress"] = {"stage": "conversation"}
            data = await fetch_conversation(self.resources.api, task_id, mode)
        files = []
        for document in data["documents"]:
            relative = f"tasks/{task_id}/conversation/{document['name']}.json"
            self.write(job, relative, encode_json(document["data"]))
            files.append(relative)
        summary = {key: value for key, value in data.items() if key != "documents"}
        summary["schema_version"] = VERSION
        summary["files"] = files
        self.write(job, f"tasks/{task_id}/conversation.json", encode_json(summary))
        results["conversation"] = {
            **summary,
            "status": "ok"
            if data["complete"] is True
            else "partial"
            if data["content_available"]
            else "error",
            "file": f"tasks/{task_id}/conversation.json",
        }
        return data

    async def gui_screenshots(
        self,
        job: dict,
        task_id: str,
        identity: TaskIdentity,
        report: dict,
        results: dict,
    ) -> None:
        """Download old screenshot references exposed by the GUI report via OpenAPI."""
        names = set()
        stack = [report]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str):
                try:
                    name = urlsplit(value).path.rsplit("/", 1)[-1]
                except ValueError:
                    continue
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    names.add(name)
        for index, name in enumerate(sorted(names)):
            job["tasks"][task_id]["progress"] = {"stage": "gui"}
            component = f"gui/screenshots/{index}"
            try:
                response = await self.resources.api.request_bytes(
                    "/task/artifacts/gui-test/screenshots",
                    {**identity.model_dump(exclude_none=True), "path": name},
                )
                suffix = Path(name).suffix.lower()
                relative = f"tasks/{task_id}/gui/screenshots/{index}{suffix}"
                self.write(job, relative, response.content)
                results[component] = {
                    "status": "ok",
                    "source": "flux_insight_openapi",
                    "file": relative,
                    "path_substring": name,
                    "warnings": ["旧截图接口按 path 子串返回首个匹配对象"],
                }
            except (ServiceError, OSError) as exc:
                results[component] = {
                    "status": "error",
                    "error": public_error(exc)
                    if isinstance(exc, ServiceError)
                    else {
                        "code": "EXPORT_WRITE_FAILED",
                        "message": "Cannot write screenshot",
                    },
                }

    async def export_task(
        self,
        job: dict,
        task_id: str,
        query: ExportQuery,
        *,
        metadata_only: bool = False,
    ) -> None:
        results = job["tasks"].get(task_id, {}).get("components", {})
        conversation_data = None
        job["tasks"][task_id] = {
            "status": "running",
            "components": results,
            "progress": {"stage": "waiting"},
        }
        try:
            async with asyncio.timeout(self.settings.export_task_timeout_seconds):
                async with self.admission:
                    identity = None
                    detail = None
                    if "task" in query.components and "task" not in results:
                        detail = await self.capture(
                            job,
                            task_id,
                            "task",
                            lambda: self.resources.task(task_id),
                            results,
                        )
                        if detail and isinstance(detail.get("tos_detail"), dict):
                            archive = detail["tos_detail"].get("content_base64")
                            if isinstance(archive, str):
                                try:
                                    content = base64.b64decode(archive, validate=True)
                                    suffix = (
                                        ".json.gz"
                                        if content.startswith(b"\x1f\x8b")
                                        else ".bin"
                                    )
                                    relative = f"tasks/{task_id}/task-snapshot{suffix}"
                                    self.write(job, relative, content)
                                    results["task"]["snapshot_file"] = relative
                                except (ValueError, ServiceError, OSError):
                                    results["task"]["status"] = "partial"
                                    results["task"]["warnings"] = [
                                        "任务归档解码或写入失败，原始响应已保留"
                                    ]
                    if metadata_only:
                        return
                    if "trace" in query.components or "metrics" in query.components:
                        try:
                            if detail is None and results.get("task", {}).get("file"):
                                try:
                                    detail = json.loads(
                                        (
                                            self.directory(job["job_id"])
                                            / f"tasks/{task_id}/task.json"
                                        ).read_bytes()
                                    )
                                except (OSError, ValueError):
                                    pass
                            job["tasks"][task_id]["progress"] = {"stage": "trace"}
                            fetched = await self.resources.trace_and_metrics(
                                TraceQuery(
                                    task_id=task_id,
                                    conversation_mode=query.conversation_mode,
                                    include_raw_spans=query.include_raw_spans,
                                    recover_raw_spans=query.recover_raw_spans,
                                    start_ms=query.start_ms,
                                    end_ms=query.end_ms,
                                ),
                                task_detail=detail,
                                want_trace="trace" in query.components,
                                want_metrics="metrics" in query.components,
                                include_conversation=(
                                    "trace" in query.components
                                    or "conversation" in query.components
                                ),
                            )
                            conversation_data = fetched.get("conversation")
                            source_files = {}
                            for source in ("openapi", "fornax"):
                                if source in fetched:
                                    source_files[source] = (
                                        f"tasks/{task_id}/trace/{source}.json"
                                    )
                                    self.write(
                                        job,
                                        source_files[source],
                                        encode_json(fetched[source]),
                                    )
                            if conversation_data is not None:
                                try:
                                    await self.conversation(
                                        job,
                                        task_id,
                                        results,
                                        conversation_data,
                                        mode=query.conversation_mode,
                                    )
                                except (ServiceError, OSError) as exc:
                                    results["conversation"] = {
                                        "status": "error",
                                        "error": public_error(exc)
                                        if isinstance(exc, ServiceError)
                                        else {
                                            "code": "EXPORT_WRITE_FAILED",
                                            "message": "Cannot write conversation data",
                                        },
                                    }
                                    if "trace" in fetched:
                                        fetched["trace"]["warnings"].append(
                                            "会话正文写入失败，执行数据仍保留"
                                        )
                            for component in ("trace", "metrics"):
                                if component not in query.components:
                                    continue
                                if component not in fetched:
                                    results[component] = {
                                        "status": "error",
                                        "errors": fetched["errors"],
                                    }
                                    continue
                                data = fetched[component]
                                relative = f"tasks/{task_id}/{component}.json"
                                stored = dict(data)
                                stored["schema_version"] = VERSION
                                if component == "trace":
                                    if "data" in stored and "openapi" in source_files:
                                        stored.pop("data")
                                        stored["data_file"] = source_files["openapi"]
                                    if conversation_data is not None:
                                        stored["conversation"] = results["conversation"]
                                    spans = stored.get("spans")
                                    if (
                                        spans
                                        and data["source"] == "flux_insight_openapi"
                                    ):
                                        stored.pop("spans")
                                        if spans is fetched.get("openapi", {}).get(
                                            "raw_spans"
                                        ):
                                            stored["spans_file"] = source_files[
                                                "openapi"
                                            ]
                                            stored["spans_pointer"] = "/raw_spans"
                                        else:
                                            raw_file = (
                                                f"tasks/{task_id}/trace/raw-spans.json"
                                            )
                                            self.write(
                                                job, raw_file, encode_json(spans)
                                            )
                                            stored["spans_file"] = raw_file
                                self.write(job, relative, encode_json(stored))
                                results[component] = {
                                    "status": "ok"
                                    if data["complete"] is True and not data["warnings"]
                                    else "partial",
                                    "source": data["source"],
                                    "complete": data["complete"],
                                    "execution_complete": data.get(
                                        "execution_complete"
                                    ),
                                    "conversation_complete": data.get(
                                        "conversation_complete"
                                    ),
                                    "raw_spans_complete": data.get(
                                        "raw_spans_complete"
                                    ),
                                    "raw_spans_requested": data.get(
                                        "raw_spans_requested"
                                    ),
                                    "raw_spans_status": data.get("raw_spans_status"),
                                    "raw_recovery": data.get("raw_recovery"),
                                    **{
                                        key: data.get(key)
                                        for key in (
                                            "token_sources",
                                            "token_field_sources",
                                            "token_scope",
                                            "timing_source",
                                            "total_latency_available",
                                            "total_latency_complete",
                                            "first_response_complete",
                                        )
                                    },
                                    "tokens_available": data.get("tokens_available"),
                                    "tokens_complete": data.get("tokens_complete"),
                                    "token_source": data.get("token_source"),
                                    "token_coverage": data.get("token_coverage"),
                                    "token_diagnostics": data.get("token_diagnostics"),
                                    "timing_complete": data.get("timing_complete"),
                                    "turn_timing_complete": data.get(
                                        "turn_timing_complete"
                                    ),
                                    "first_response_available": data.get(
                                        "first_response_available"
                                    ),
                                    "timing_coverage": data.get("timing_coverage"),
                                    "timing_diagnostics": data.get(
                                        "timing_diagnostics"
                                    ),
                                    "file": relative,
                                    "warnings": data["warnings"],
                                    "fallback_errors": fetched["errors"],
                                }
                                if component == "trace" and "tree" in data:
                                    self.write(
                                        job,
                                        f"tasks/{task_id}/trace.tree.txt",
                                        data["tree"].encode(),
                                    )
                        except (ServiceError, OSError) as exc:
                            for component in ("trace", "metrics"):
                                if component in query.components:
                                    results.setdefault(
                                        component,
                                        {
                                            "status": "error",
                                            "error": public_error(exc)
                                            if isinstance(exc, ServiceError)
                                            else {
                                                "code": "EXPORT_WRITE_FAILED",
                                                "message": "Cannot write trace data",
                                            },
                                        },
                                    )
                    for component in ("artifacts", "versions", "gui"):
                        if component not in query.components:
                            continue
                        try:
                            if identity is None:
                                identity = TaskIdentity(task_id=task_id)
                            if component == "artifacts":
                                manifest = await self.capture(
                                    job,
                                    task_id,
                                    "artifacts/manifest",
                                    lambda identity=identity: self.resources.artifacts(
                                        identity
                                    ),
                                    results,
                                )
                                if manifest is None:
                                    continue
                                session_id = manifest.get(
                                    "worker_session_id"
                                ) or manifest.get("session_id")
                                if session_id:
                                    identity = TaskIdentity(
                                        worker_session_id=session_id
                                    )
                                downloaded = []
                                downloader = ArtifactDownloads(
                                    self.resources, identity, manifest
                                )
                                progress = {
                                    "stage": "artifacts",
                                    "file_count": len(manifest["files"]),
                                    "completed_count": 0,
                                    "downloaded_count": 0,
                                    "failed_count": 0,
                                }
                                job["tasks"][task_id]["progress"] = progress
                                for item in manifest["files"]:
                                    path = item["path"]
                                    try:
                                        content, record = await downloader.download(
                                            item
                                        )
                                        if content is None:
                                            downloaded.append(record)
                                            continue
                                        relative = (
                                            f"tasks/{task_id}/artifacts/files/"
                                            + safe_artifact_path(path)
                                        )
                                        self.write(job, relative, content)
                                        record["file"] = relative
                                        downloaded.append(record)
                                        progress["downloaded_count"] += 1
                                    except (ServiceError, OSError) as exc:
                                        downloaded.append(
                                            {
                                                "path": path,
                                                "status": "error",
                                                "error": public_error(exc)
                                                if isinstance(exc, ServiceError)
                                                else {
                                                    "code": "EXPORT_WRITE_FAILED",
                                                    "message": "Cannot write artifact",
                                                },
                                            }
                                        )
                                    finally:
                                        progress["completed_count"] = len(downloaded)
                                        progress["failed_count"] = (
                                            len(downloaded)
                                            - progress["downloaded_count"]
                                        )
                                job["tasks"][task_id].pop("progress", None)
                                refreshed_file = None
                                if downloader.refreshed is not None:
                                    refreshed_file = (
                                        f"tasks/{task_id}/artifacts/"
                                        "manifest-refreshed.json"
                                    )
                                    self.write(
                                        job,
                                        refreshed_file,
                                        encode_json(downloader.refreshed),
                                    )
                                self.write(
                                    job,
                                    f"tasks/{task_id}/artifacts/downloads.json",
                                    encode_json(downloaded),
                                )
                                results["artifacts"] = {
                                    "status": "partial"
                                    if any(
                                        item["status"] == "error" for item in downloaded
                                    )
                                    else "ok",
                                    "source": "flux_insight_openapi",
                                    "file_count": len(downloaded),
                                    "downloaded_count": sum(
                                        item["status"] == "ok" for item in downloaded
                                    ),
                                    "errors": {
                                        item["path"]: item["error"]
                                        for item in downloaded
                                        if item["status"] == "error"
                                    },
                                    "file": f"tasks/{task_id}/artifacts/downloads.json",
                                    "worker_session_id": identity.worker_session_id,
                                    "selection_policy": "initial-manifest-paths",
                                    "manifest_refresh_attempted": (
                                        downloader.refresh_attempted
                                    ),
                                    "refreshed_manifest_file": refreshed_file,
                                    "manifest_refresh_error": downloader.refresh_error,
                                    "recovered_count": sum(
                                        item.get("recovered", False)
                                        for item in downloaded
                                    ),
                                    "manifest_changed_count": sum(
                                        item.get("manifest_changed", False)
                                        for item in downloaded
                                    ),
                                }
                            elif component == "versions":
                                versions = await self.capture(
                                    job,
                                    task_id,
                                    "versions",
                                    lambda identity=identity: (
                                        self.resources.api.request_json(
                                            "/task/artifacts/versions",
                                            identity.model_dump(exclude_none=True),
                                        )
                                    ),
                                    results,
                                )
                                if versions is not None:
                                    comparisons = versions.get("comparisons")
                                    if (
                                        not isinstance(comparisons, list)
                                        or not comparisons
                                    ):
                                        results["versions"]["warnings"] = [
                                            "版本接口未枚举可用 diff 区间；"
                                            "指定区间可调用 "
                                            "/v1/task-data/artifacts/version-diff"
                                        ]
                                    else:
                                        for index, comparison in enumerate(comparisons):
                                            if isinstance(comparison, dict) and all(
                                                key in comparison
                                                for key in (
                                                    "from_version_id",
                                                    "to_version_id",
                                                )
                                            ):
                                                body = {
                                                    **identity.model_dump(
                                                        exclude_none=True
                                                    ),
                                                    "from_version_id": comparison[
                                                        "from_version_id"
                                                    ],
                                                    "to_version_id": comparison[
                                                        "to_version_id"
                                                    ],
                                                }
                                                await self.capture(
                                                    job,
                                                    task_id,
                                                    f"diffs/{index}",
                                                    lambda body=body: (
                                                        self.resources.api.request_json(
                                                            "/task/artifacts/version-diff",
                                                            body,
                                                        )
                                                    ),
                                                    results,
                                                )
                            else:
                                gui = await self.capture(
                                    job,
                                    task_id,
                                    "gui",
                                    lambda identity=identity: (
                                        self.resources.api.request_json(
                                            "/task/artifacts/gui-test/case-result",
                                            identity.model_dump(exclude_none=True),
                                        )
                                    ),
                                    results,
                                )
                                if gui is not None:
                                    await self.gui_screenshots(
                                        job, task_id, identity, gui, results
                                    )
                        except (ValidationError, ServiceError, OSError) as exc:
                            job["tasks"][task_id].pop("progress", None)
                            results[component] = {
                                "status": "error",
                                "error": public_error(exc)
                                if isinstance(exc, ServiceError)
                                else {
                                    "code": "EXPORT_COMPONENT_FAILED",
                                    "message": "Invalid identity or write failed",
                                },
                            }
                    if "conversation" not in results and (
                        "conversation" in query.components
                        or conversation_data is not None
                    ):
                        await self.conversation(
                            job,
                            task_id,
                            results,
                            conversation_data,
                            mode=query.conversation_mode,
                        )
        except TimeoutError:
            results["task" if metadata_only else "timeout"] = {
                "status": "error",
                "error": {
                    "code": "QUERY_TIMEOUT",
                    "message": "Task export deadline exceeded",
                },
            }
        except asyncio.CancelledError:
            job["tasks"][task_id]["status"] = "cancelled"
            raise
        except (ServiceError, OSError, ValueError, TypeError, AttributeError):
            results["task" if metadata_only else "export"] = {
                "status": "error",
                "error": {
                    "code": "EXPORT_COMPONENT_FAILED",
                    "message": "Task response or export output invalid",
                },
            }
        finally:
            record = job["tasks"][task_id]
            record.pop("progress", None)
            if metadata_only:
                if record["status"] != "cancelled":
                    record["status"] = "queued"
            elif record["status"] != "cancelled":
                statuses = [value["status"] for value in results.values()]
                record["status"] = (
                    "ok"
                    if statuses and all(value == "ok" for value in statuses)
                    else "partial"
                    if any(value in {"ok", "partial"} for value in statuses)
                    else "error"
                )
            if not metadata_only:
                job["completed_count"] += 1
            self.save_task_manifest(job, task_id, query, finalized=not metadata_only)
            self.save(job)

    def package(self, job: dict) -> None:
        root = self.directory(job["job_id"])
        temporary = root / "export.zip.tmp"
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.writestr("job.json", encode_json({**job, "archive_ready": True}))
            for path in sorted(root.rglob("*")):
                relative = path.relative_to(root).as_posix()
                if path.is_file() and relative not in {
                    "export.zip",
                    "export.zip.tmp",
                    "job.json.tmp",
                    "job.json",
                }:
                    archive.write(path, relative)
        temporary.replace(root / "export.zip")

    async def run(
        self, job: dict, query: ExportQuery, *, refresh: bool = False
    ) -> None:
        ids = query.task_ids
        try:
            if refresh:
                root = self.directory(job["job_id"])
                for task_id in ids:
                    task_root = root / "tasks" / task_id
                    if task_root.exists():
                        shutil.rmtree(task_root)
                job["bytes_written"] = sum(
                    path.stat().st_size
                    for path in root.rglob("*")
                    if path.is_file()
                    and path.relative_to(root).as_posix()
                    not in {"job.json", "job.json.tmp", "export.zip", "export.zip.tmp"}
                )
            if job.get("cancel_requested"):
                raise asyncio.CancelledError
            job["status"] = "running"
            self.save(job)
            if query.filters is not None:
                async with asyncio.timeout(self.settings.query_timeout_seconds):
                    async with self.admission:
                        collected = await self.tasks.collect(
                            CollectQuery(filters=query.filters)
                        )
                        ids = collected.task_ids
                        self.write(
                            job, "selection.json", encode_json(collected.model_dump())
                        )
            if not refresh:
                job["task_count"] = len(ids)
            self.write(
                job,
                "task_ids.json",
                encode_json(job["request"]["task_ids"] if refresh else ids),
            )
            for task_id in ids:
                job["tasks"][task_id] = {"status": "queued", "components": {}}
                self.save_task_manifest(job, task_id, query, finalized=False)

            async def process_tasks(*, metadata_only: bool = False):
                queue = asyncio.Queue()
                for task_id in ids:
                    queue.put_nowait(task_id)

                async def worker():
                    while not queue.empty():
                        task_id = queue.get_nowait()
                        await self.export_task(
                            job, task_id, query, metadata_only=metadata_only
                        )

                async with asyncio.TaskGroup() as group:
                    for _ in range(min(len(ids), self.settings.max_concurrent_queries)):
                        group.create_task(worker())

            if "task" in query.components:
                await process_tasks(metadata_only=True)
            await process_tasks()
            job["status"] = (
                "completed"
                if all(item["status"] == "ok" for item in job["tasks"].values())
                else "partial"
            )
        except asyncio.CancelledError:
            job["status"] = "cancelled"
        except TimeoutError:
            job["status"] = "failed"
            job["error"] = {
                "code": "QUERY_TIMEOUT",
                "message": "Export selection deadline exceeded",
            }
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = (
                public_error(exc)
                if isinstance(exc, ServiceError)
                else {
                    "code": "EXPORT_FAILED",
                    "message": "Export selection or processing failed",
                }
            )
        finally:
            try:
                job["finished_at"] = int(time.time() * 1000)
                for task_id in ids:
                    record = job["tasks"].setdefault(
                        task_id, {"status": "queued", "components": {}}
                    )
                    if record["status"] in {"queued", "running"}:
                        record["status"] = (
                            "cancelled" if job["status"] == "cancelled" else "error"
                        )
                    self.save_task_manifest(job, task_id, query, finalized=True)
                if job["status"] == "completed" and any(
                    record["status"] != "ok" for record in job["tasks"].values()
                ):
                    job["status"] = "partial"
                self.save(job)
                packaging = asyncio.create_task(asyncio.to_thread(self.package, job))
                try:
                    await asyncio.shield(packaging)
                except asyncio.CancelledError:
                    await packaging
                job["archive_ready"] = True
                self.save(job)
            except (OSError, ServiceError, ValueError):
                job["status"] = "failed"
                job["archive_ready"] = False
                job["error"] = {
                    "code": "EXPORT_WRITE_FAILED",
                    "message": "Cannot validate or write export manifest/archive",
                }
                try:
                    self.save(job)
                except OSError:
                    pass
            self.running.pop(job["job_id"], None)
            # Terminal jobs are read from disk, avoiding unbounded process retention.
            self.jobs.pop(job["job_id"], None)

    def _cancel_job(self, job: dict, job_id: str) -> dict:
        task = self.running.get(job_id)
        if task is not None and job["status"] in {"queued", "running"}:
            job["cancel_requested"] = True
            if job["status"] == "running":
                task.cancel()
        return job

    def cancel(self, job_id: str) -> dict:
        return self._cancel_job(self.get(job_id), job_id)

    async def cancel_async(self, job_id: str) -> dict:
        return self._cancel_job(await self.get_async(job_id), job_id)

    async def close(self) -> None:
        running = list(self.running.values())
        for job_id in list(self.running):
            self.cancel(job_id)
        await asyncio.gather(*running, return_exceptions=True)
