"""Small read models for browsing tasks, jobs and saved task datasets."""

import asyncio
import re
from collections import Counter
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import Field, field_validator

from flux_task_service.errors import ServiceError
from flux_task_service.export_contract import ContractError, TaskDataReader
from flux_task_service.models import Filters, RequestModel, TaskID


class SummaryQuery(RequestModel):
    task_ids: list[TaskID] = Field(min_length=1, max_length=200)

    @field_validator("task_ids")
    @classmethod
    def nonzero_ids(cls, values):
        if any(not value.strip("0") for value in values):
            raise ValueError("Task IDs must be nonzero")
        return values


def issue(record: dict) -> bool:
    return record.get("status") in {
        "partial",
        "error",
        "cancelled",
        "interrupted",
        "not_started",
    } or any(
        value.get("status") in {"partial", "error"}
        or any(
            value.get(key) for key in ("error", "errors", "warnings", "fallback_errors")
        )
        for value in record.get("components", {}).values()
    )


def overview(job: dict) -> dict:
    result = {
        key: job[key]
        for key in (
            "job_id",
            "status",
            "created_at",
            "finished_at",
            "task_count",
            "completed_count",
            "bytes_written",
            "archive_ready",
            "components",
            "warnings",
            "error",
            "retry_of",
        )
        if key in job
    }
    tasks = job.get("tasks", {})
    counts = Counter(value.get("status", "queued") for value in tasks.values())
    missing = max(0, job.get("task_count", 0) - len(tasks))
    active = job.get("status") in {"queued", "running", "packaging"}
    if not active:
        counts["stopped"] += counts.pop("running", 0) + counts.pop("queued", 0)
    counts["queued" if active else "stopped"] += missing
    progress = [
        value["progress"]
        for value in tasks.values()
        if value.get("status") == "running" and value.get("progress")
    ]
    result.update(
        task_counts=dict(counts),
        stages=dict(Counter(value["stage"] for value in progress)) if active else {},
        file_progress={
            key: sum(
                value.get(key, 0) for value in progress if value["stage"] == "artifacts"
            )
            for key in ("file_count", "completed_count", "failed_count")
        },
    )
    return result


def workspace_router(config, authorize) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorize)])

    @asynccontextmanager
    async def deadline():
        try:
            async with asyncio.timeout(config.query_timeout_seconds):
                yield
        except TimeoutError as exc:
            raise ServiceError("QUERY_TIMEOUT", "Summary query timed out", 504) from exc

    @router.post("/v1/tasks/summaries", tags=["任务信息"])
    async def summaries(query: SummaryQuery, request: Request) -> dict:
        service = request.app.state.service
        ids = list(dict.fromkeys(query.task_ids))
        rows = {}
        cursor = None
        seen = set()
        async with deadline():
            async with request.app.state.admission:
                for _ in range(config.max_pages):
                    page = await service.raw_page(
                        Filters(task_ids=ids), 50 if len(ids) == 1 else 200, cursor
                    )
                    for item in page.tasks:
                        task = item.get("task") or {}
                        if not isinstance(task, dict):
                            raise ServiceError(
                                "UPSTREAM_PROTOCOL_ERROR", "Invalid task summary"
                            )
                        rows[item["task_id"]] = {
                            "task_id": item["task_id"],
                            "title": task.get("title") or task.get("task_title"),
                            "status": task.get("task_status") or task.get("status"),
                            "owner": task.get("created_by"),
                            "created_at": task.get("task_created_at")
                            or task.get("created_at"),
                        }
                    if not page.has_more or len(rows) == len(ids):
                        break
                    cursor = service.advance_cursor(page.next_cursor, seen)
        return {"tasks": rows, "missing": [tid for tid in ids if tid not in rows]}

    @router.get("/v1/exports", tags=["批量导出"])
    async def history(
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict:
        manager = request.app.state.exports
        live = {job_id: overview(job) for job_id, job in manager.jobs.items()}

        def read():
            ids = set(live)
            ids.update(
                path.name
                for path in config.export_dir.glob("*")
                if re.fullmatch(r"[a-f0-9]{32}", path.name) and not path.is_symlink()
            )
            jobs = []
            for job_id in ids:
                try:
                    jobs.append(
                        live[job_id]
                        if job_id in live
                        else overview(manager.get(job_id))
                    )
                except (ServiceError, OSError, ValueError, TypeError, AttributeError):
                    continue
            jobs.sort(
                key=lambda job: (job.get("created_at") or 0, job["job_id"]),
                reverse=True,
            )
            return {"jobs": jobs[offset : offset + limit], "total": len(jobs)}

        return await asyncio.to_thread(read)

    @router.get("/v1/exports/{job_id}/overview", tags=["批量导出"])
    async def status(job_id: str, request: Request) -> dict:
        return overview(await request.app.state.exports.get_async(job_id))

    @router.get("/v1/exports/{job_id}/tasks", tags=["批量导出"])
    async def tasks(
        job_id: str,
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        search: str = Query("", max_length=64),
        status: Literal["all", "active", "issues", "ok"] = "all",
    ) -> dict:
        job = await request.app.state.exports.get_async(job_id)
        stopped = job["status"] not in {"queued", "running", "packaging"}
        entries = []
        for tid, original in job.get("tasks", {}).items():
            record = original
            if stopped and record.get("status") in {"queued", "running"}:
                record = {
                    **record,
                    "status": "interrupted"
                    if job["status"] != "cancelled"
                    else "cancelled",
                }
            if search not in tid:
                continue
            if status == "active" and record.get("status") not in {"queued", "running"}:
                continue
            if status == "issues" and not issue(record):
                continue
            if status == "ok" and (record.get("status") != "ok" or issue(record)):
                continue
            entries.append((tid, record))
        return {
            "tasks": dict(entries[offset : offset + limit]),
            "total": len(entries),
            "offset": offset,
            "limit": limit,
        }

    def saved_reader(manager, job_id: str, task_id: str) -> TaskDataReader:
        job = manager.get(job_id)
        record = job.get("tasks", {}).get(task_id)
        if record is None:
            raise ServiceError(
                "EXPORT_TASK_NOT_FOUND", "Task is not in this batch", 404
            )
        if record.get("status") in {"queued", "running"}:
            raise ServiceError("EXPORT_NOT_READY", "Task is still being fetched", 409)
        root = manager.directory(job_id).resolve()
        task_root = (root / "tasks" / task_id).resolve()
        if not root.is_relative_to(
            config.export_dir.resolve()
        ) or not task_root.is_relative_to(root):
            raise ServiceError("EXPORT_TASK_NOT_FOUND", "Invalid task directory", 404)
        reader = TaskDataReader(task_root)
        if not reader.manifest.finalized:
            raise ServiceError(
                "EXPORT_NOT_READY", "Task snapshot is not finalized", 409
            )
        return reader

    async def read_saved(operation):
        try:
            return await asyncio.to_thread(operation)
        except (ContractError, OSError) as exc:
            raise ServiceError(
                "SAVED_DATA_UNAVAILABLE", "Saved task data is unavailable", 409
            ) from exc

    @router.get(
        "/v1/exports/{job_id}/tasks/{task_id}/datasets/{dataset}", tags=["批量导出"]
    )
    async def dataset(
        job_id: str,
        task_id: TaskID,
        dataset: Literal[
            "task",
            "execution",
            "conversation",
            "metrics",
            "artifacts",
            "versions",
            "gui",
        ],
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> dict:
        def read():
            reader = saved_reader(request.app.state.exports, job_id, task_id)
            entry = reader.manifest.datasets[dataset]
            refs = [ref for ref in entry.files if ref.format != "bytes.v1"]
            record = request.app.state.exports.get(job_id)["tasks"][task_id]
            names = {
                value["file"].removeprefix(f"tasks/{task_id}/"): value["path_substring"]
                for value in record.get("components", {}).values()
                if value.get("file") and value.get("path_substring")
            }
            return {
                **entry.model_dump(),
                "file_names": names,
                "documents": [
                    {"reference": ref.model_dump(), "content": reader.read_file(ref)}
                    for ref in refs[offset : offset + limit]
                ],
                "total": len(refs),
                "offset": offset,
                "has_more": offset + limit < len(refs),
            }

        return await read_saved(read)

    @router.get("/v1/exports/{job_id}/tasks/{task_id}/file", tags=["批量导出"])
    async def file(
        job_id: str,
        task_id: TaskID,
        request: Request,
        path: str = Query(..., min_length=1, max_length=4096),
    ) -> FileResponse:
        def read():
            reader = saved_reader(request.app.state.exports, job_id, task_id)
            ref = next(
                (
                    ref
                    for entry in reader.manifest.datasets.values()
                    for ref in entry.files
                    if ref.path == path and ref.format == "bytes.v1"
                ),
                None,
            )
            if ref is None:
                raise ServiceError(
                    "SAVED_FILE_NOT_FOUND", "File is not in this snapshot", 404
                )
            target = (reader.root / ref.path).resolve()
            if not target.is_relative_to(reader.root) or not target.is_file():
                raise ServiceError(
                    "SAVED_FILE_NOT_FOUND", "Saved file is unavailable", 404
                )
            return FileResponse(
                target,
                filename=target.name,
                media_type="application/octet-stream",
                headers={"X-Content-Type-Options": "nosniff"},
            )

        return await read_saved(read)

    return router
