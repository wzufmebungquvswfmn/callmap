"""HTTP endpoints for task data and background export jobs."""

import asyncio
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from starlette.background import BackgroundTask

from flux_task_service.chatml import MessageRole, OutputMode, load_trace
from flux_task_service.config import Settings
from flux_task_service.errors import ServiceError
from flux_task_service.export_contract import ContractError
from flux_task_service.models import (
    ArtifactQuery,
    ConversationQuery,
    ExportJobResponse,
    ExportQuery,
    ExportRetryQuery,
    FornaxAuthQuery,
    FornaxAuthResponse,
    MetricsQuery,
    MetricsResponse,
    TaskDataQuery,
    TaskID,
    TaskIdentity,
    TraceQuery,
    TraceResponse,
    VersionDiffQuery,
)
from flux_task_service.workspace_api import workspace_router


def data_router(config: Settings, authorize) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorize)])
    router.include_router(workspace_router(config, authorize))

    async def run(request: Request, operation):
        try:
            async with asyncio.timeout(config.export_task_timeout_seconds):
                async with request.app.state.admission:
                    return await operation()
        except TimeoutError as exc:
            raise ServiceError(
                "QUERY_TIMEOUT", "Task data query deadline exceeded", 504
            ) from exc

    @router.post(
        "/v1/task-data/fornax/auth",
        tags=["任务信息"],
        summary="更新、重读或刷新 Fornax 个人凭据",
        response_model=FornaxAuthResponse,
        response_model_exclude_unset=True,
    )
    async def fornax_auth(query: FornaxAuthQuery, request: Request) -> dict:
        client = request.app.state.resources.fornax
        if query.action == "update":
            await client.update_token(query.jwt.get_secret_value())
        else:
            await run(
                request,
                lambda: client.auth_token(
                    reload=query.action == "reload", refresh=query.action == "refresh"
                ),
            )
        return {"status": "loaded", "action": query.action, "upstream_verified": False}

    @router.post(
        "/v1/task-data/detail", tags=["任务信息"], summary="获取任务详情及可选归档原文"
    )
    async def detail(query: TaskDataQuery, request: Request) -> dict:
        return await run(
            request, lambda: request.app.state.resources.task(query.task_id)
        )

    @router.post(
        "/v1/task-data/trace",
        tags=["任务信息"],
        summary="获取 Trace：OpenAPI 优先，Fornax 兜底",
        response_model=TraceResponse,
        response_model_exclude_unset=True,
    )
    async def trace(query: TraceQuery, request: Request) -> dict:
        data = await run(
            request,
            lambda: request.app.state.resources.trace_and_metrics(
                query, want_metrics=False, include_conversation=True
            ),
        )
        if "trace" not in data:
            raise ServiceError(
                "TRACE_UNAVAILABLE",
                "No usable trace from the requested sources",
                502,
                fallback_errors=data["errors"],
            )
        result = {
            "task_id": query.task_id,
            **data["trace"],
            "fallback_errors": data["errors"],
        }
        if result["source"] == "fornax" and "openapi" in data:
            result["openapi_data"] = data["openapi"]
        return result

    @router.post(
        "/v1/task-data/metrics",
        tags=["任务信息"],
        summary="获取 Token 与耗时指标：OpenAPI 优先，Fornax 兜底",
        response_model=MetricsResponse,
        response_model_exclude_unset=True,
    )
    async def metrics(query: MetricsQuery, request: Request) -> dict:
        trace_query = TraceQuery(
            task_id=query.task_id,
            source=query.source,
            start_ms=query.start_ms,
            end_ms=query.end_ms,
        )
        data = await run(
            request,
            lambda: request.app.state.resources.trace_and_metrics(
                trace_query, want_trace=False
            ),
        )
        if "metrics" not in data:
            raise ServiceError(
                "METRICS_UNAVAILABLE",
                "No usable metrics from the requested sources",
                502,
                fallback_errors=data["errors"],
            )
        return {
            "task_id": query.task_id,
            **data["metrics"],
            "fallback_errors": data["errors"],
        }

    @router.post(
        "/v1/task-data/artifacts/list",
        tags=["产物"],
        summary="获取 OpenAPI 已上传文件清单",
    )
    async def artifacts(query: TaskIdentity, request: Request) -> dict:
        return await run(request, lambda: request.app.state.resources.artifacts(query))

    @router.post(
        "/v1/task-data/artifacts/raw",
        tags=["产物"],
        summary="通过 OpenAPI 下载指定路径的原始文件",
        response_class=Response,
        responses={200: {"content": {"application/octet-stream": {}}}},
    )
    async def raw(query: ArtifactQuery, request: Request) -> Response:
        identity = TaskIdentity(
            task_id=query.task_id, worker_session_id=query.worker_session_id
        )
        result = await run(
            request, lambda: request.app.state.resources.artifact(identity, query.path)
        )
        headers = {
            key: value
            for key, value in result.headers.items()
            if key.startswith("x-artifact-")
        }
        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(
            query.path.rsplit("/", 1)[-1], safe=""
        )
        return Response(
            result.content,
            media_type=result.headers.get("Content-Type", "application/octet-stream"),
            headers=headers,
        )

    @router.post(
        "/v1/task-data/artifacts/versions", tags=["产物"], summary="获取已采集版本信息"
    )
    async def versions(query: TaskIdentity, request: Request) -> dict:
        return await run(
            request,
            lambda: request.app.state.resources.api.request_json(
                "/task/artifacts/versions", query.model_dump(exclude_none=True)
            ),
        )

    @router.post(
        "/v1/task-data/artifacts/version-diff",
        tags=["产物"],
        summary="获取指定已采集区间的 Diff",
    )
    async def diff(query: VersionDiffQuery, request: Request) -> dict:
        return await run(
            request,
            lambda: request.app.state.resources.api.request_json(
                "/task/artifacts/version-diff", query.model_dump(exclude_none=True)
            ),
        )

    @router.post(
        "/v1/task-data/artifacts/gui-test/case-result",
        tags=["产物"],
        summary="获取已采集 GUI 用例结果",
    )
    async def gui(query: TaskIdentity, request: Request) -> dict:
        return await run(
            request,
            lambda: request.app.state.resources.api.request_json(
                "/task/artifacts/gui-test/case-result",
                query.model_dump(exclude_none=True),
            ),
        )

    @router.post(
        "/v1/task-data/artifacts/gui-test/screenshots",
        tags=["产物"],
        summary="下载旧采集截图（path 子串匹配）",
        response_class=Response,
        responses={200: {"content": {"application/octet-stream": {}}}},
    )
    async def screenshots(query: ArtifactQuery, request: Request) -> Response:
        result = await run(
            request,
            lambda: request.app.state.resources.api.request_bytes(
                "/task/artifacts/gui-test/screenshots",
                query.model_dump(exclude_none=True),
            ),
        )
        return Response(
            result.content,
            media_type=result.headers.get("Content-Type", "application/octet-stream"),
        )

    @router.post(
        "/v1/task-data/conversation",
        tags=["任务信息"],
        summary="通过 OpenAPI 分批读取原文或消息（需要敏感权限）",
    )
    async def conversation(query: ConversationQuery, request: Request) -> dict:
        return await run(
            request,
            lambda: request.app.state.resources.api.request_json(
                "/task/trace", query.model_dump(exclude_none=True)
            ),
        )

    @router.post(
        "/v1/exports",
        status_code=202,
        tags=["批量导出"],
        summary="从 task ID 或筛选条件创建后台导出",
        response_model=ExportJobResponse,
        response_model_exclude_unset=True,
    )
    async def create_export(query: ExportQuery, request: Request) -> dict:
        return request.app.state.exports.create(query)

    @router.get(
        "/v1/exports/{job_id}",
        tags=["批量导出"],
        summary="查询导出进度与每个任务的结果",
        response_model=ExportJobResponse,
        response_model_exclude_unset=True,
    )
    async def export_status(
        job_id: str,
        request: Request,
        include_tasks: bool = Query(default=True),
        task_id: str | None = Query(default=None, pattern=r"^[1-9][0-9]{0,63}$"),
        offset: int = Query(default=0, ge=0),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ) -> dict:
        manager = request.app.state.exports
        job = await manager.get_async(job_id)
        return manager.page(
            job,
            include_tasks=include_tasks,
            task_id=task_id,
            offset=offset,
            limit=limit,
        )

    @router.get(
        "/v1/chatml/{job_id}/{task_id}",
        tags=["批量导出"],
        summary="将已导出任务转换为 ChatML 文本",
        description="从任务数据清单读取对话，不重新请求上游。"
        "assistant 模式仅保留回复和过程说明，过滤推理与工具调用。",
        response_class=PlainTextResponse,
        responses={
            404: {"description": "导出批次或任务不存在"},
            409: {"description": "任务尚未完成或对话数据无法转换"},
        },
    )
    async def chatml(
        job_id: str,
        task_id: TaskID,
        request: Request,
        mode: Annotated[list[OutputMode] | None, Query()] = None,
        exclude_role: Annotated[list[MessageRole] | None, Query()] = None,
        visible_escapes: bool = Query(default=False),
        allow_legacy: bool = Query(default=False),
    ) -> PlainTextResponse:
        modes = list(dict.fromkeys(mode or ["all"]))
        excluded = list(dict.fromkeys(exclude_role or []))

        def render() -> PlainTextResponse:
            manager = request.app.state.exports
            root = manager.directory(job_id).resolve()
            task_root = root / "tasks" / task_id
            if not root.is_relative_to(config.export_dir.resolve()) or not (
                task_root.resolve().is_relative_to(root)
            ):
                raise ServiceError(
                    "EXPORT_TASK_NOT_FOUND", "Export task directory not found", 404
                )
            job = manager.get(job_id)
            manager.page(job, task_id=task_id)
            if job["tasks"][task_id].get("status") in {"queued", "running"}:
                raise ServiceError(
                    "EXPORT_NOT_READY", "Task export is still unfinished", 409
                )
            if not task_root.is_dir():
                raise ServiceError(
                    "CHATML_UNAVAILABLE", "Task conversation data is unavailable", 409
                )
            try:
                transcript = load_trace(task_root, allow_legacy=allow_legacy)
                text = transcript.render(
                    mode=modes,
                    exclude_roles=excluded,
                    visible_escapes=visible_escapes,
                )
            except ContractError as exc:
                if exc.code == "MANIFEST_NOT_FINALIZED":
                    raise ServiceError(
                        "EXPORT_NOT_READY", "Task export is still unfinished", 409
                    ) from exc
                raise ServiceError(
                    "CHATML_UNAVAILABLE",
                    "Task conversation data cannot be read; check the export manifest",
                    409,
                ) from exc
            except ValueError as exc:
                raise ServiceError(
                    "CHATML_UNAVAILABLE",
                    "Task has no usable conversation data for ChatML conversion",
                    409,
                ) from exc
            except OSError as exc:
                raise ServiceError(
                    "CHATML_READ_FAILED", "Failed to read task conversation data", 500
                ) from exc
            return PlainTextResponse(
                text,
                headers={
                    "Cache-Control": "no-store",
                    "X-ChatML-Mode": ",".join(modes),
                    "X-ChatML-Excluded-Roles": ",".join(excluded),
                    "X-ChatML-Messages": str(
                        len(transcript.selected_messages(modes, exclude_roles=excluded))
                    ),
                    "X-ChatML-Source-Messages": str(len(transcript.messages)),
                    "X-ChatML-Warnings": str(len(transcript.warnings)),
                },
            )

        return await run(request, lambda: asyncio.to_thread(render))

    @router.get(
        "/v1/exports/{job_id}/download",
        tags=["批量导出"],
        summary="下载原始导出 ZIP，可按 task_id 选择单个或多个任务",
        response_class=FileResponse,
        responses={200: {"content": {"application/zip": {}}}},
    )
    async def download(
        job_id: str,
        request: Request,
        task_id: Annotated[
            list[str] | None,
            Query(description="默认完整下载；多选重复参数：task_id=111&task_id=222"),
        ] = None,
    ) -> FileResponse:
        manager = request.app.state.exports
        path = await asyncio.to_thread(manager.download, job_id, task_id)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"flux_export_{job_id}.zip",
            background=BackgroundTask(path.unlink, missing_ok=True)
            if task_id is not None
            else None,
        )

    @router.post(
        "/v1/exports/{job_id}/cancel",
        tags=["批量导出"],
        summary="取消导出并保留已经拉取的信息",
        response_model=ExportJobResponse,
        response_model_exclude_unset=True,
    )
    async def cancel(
        job_id: str,
        request: Request,
        include_tasks: bool = Query(default=True),
        offset: int = Query(default=0, ge=0),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ) -> dict:
        manager = request.app.state.exports
        job = await manager.cancel_async(job_id)
        return manager.page(
            job, include_tasks=include_tasks, offset=offset, limit=limit
        )

    @router.post(
        "/v1/exports/{job_id}/retry",
        status_code=202,
        tags=["批量导出"],
        summary="重新获取原批次的全部已选任务，更新原批次",
        response_model=ExportJobResponse,
        response_model_exclude_unset=True,
    )
    async def retry_batch(
        job_id: str,
        request: Request,
        query: ExportRetryQuery | None = None,
        include_tasks: bool = Query(default=True),
        offset: int = Query(default=0, ge=0),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ) -> dict:
        manager = request.app.state.exports
        job = await manager.retry_async(job_id, query or ExportRetryQuery())
        return manager.page(
            job, include_tasks=include_tasks, offset=offset, limit=limit
        )

    @router.post(
        "/v1/exports/{job_id}/tasks/{task_id}/retry",
        status_code=202,
        tags=["批量导出"],
        summary="重新获取原批次中的单个任务，更新原批次",
        response_model=ExportJobResponse,
        response_model_exclude_unset=True,
    )
    async def retry_task(
        job_id: str,
        task_id: TaskID,
        request: Request,
        query: ExportRetryQuery | None = None,
        include_tasks: bool = Query(default=True),
        offset: int = Query(default=0, ge=0),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ) -> dict:
        manager = request.app.state.exports
        job = await manager.retry_async(
            job_id, query or ExportRetryQuery(), task_id=task_id
        )
        return manager.page(
            job, include_tasks=include_tasks, offset=offset, limit=limit
        )

    return router
