"""Authenticated tagging APIs reference completed server export IDs only."""

import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from .models import TaggingJobResponse, TaggingResultsPage
from .service import TaggingQuery
from .tag_schema import SCHEMA_VERSION, DynamicTaskTags


def tagging_router(authorize) -> APIRouter:
    router = APIRouter(dependencies=[Depends(authorize)], tags=["测试造题筛选"])

    @router.get("/v1/tagging/tag-schema", summary="获取可编辑标签白名单与 JSON Schema")
    async def tag_schema() -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "replacement": "entire_tag_map",
            "precondition": "GET ETag, merge locally, POST with If-Match",
            "schema": DynamicTaskTags.model_json_schema(),
        }

    @router.get(
        "/v1/tagging/{tagging_id}/task/{task_id}",
        summary="读取单任务可编辑标签；ETag 用于覆盖写入",
        response_model=DynamicTaskTags,
        response_model_exclude_unset=True,
    )
    async def editable_tags(
        tagging_id: str,
        task_id: str,
        request: Request,
        response: Response,
    ) -> dict:
        snapshot = await asyncio.to_thread(
            request.app.state.tagging.editable_tags, tagging_id, task_id
        )
        response.headers["ETag"] = snapshot.etag(tagging_id, task_id)
        response.headers["Cache-Control"] = "no-store"
        return snapshot.tags

    @router.post(
        "/v1/tagging/{tagging_id}/task/{task_id}",
        summary="覆盖单任务整份可编辑标签；未提交的 tag 将被移除",
        response_model=DynamicTaskTags,
        response_model_exclude_unset=True,
        responses={
            428: {"description": "必须先 GET 并提供 If-Match"},
            412: {"description": "版本过期，需重新 GET、合并后提交"},
        },
    )
    async def update_tags(
        tagging_id: str,
        task_id: str,
        tags: DynamicTaskTags,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, max_length=128),
    ) -> dict:
        snapshot = await asyncio.to_thread(
            request.app.state.tagging.update_tags,
            tagging_id,
            task_id,
            tags.model_dump(mode="json", exclude_unset=True),
            if_match,
        )
        response.headers["ETag"] = snapshot.etag(tagging_id, task_id)
        response.headers["Cache-Control"] = "no-store"
        return snapshot.tags

    @router.post(
        "/v1/exports/{job_id}/tagging",
        status_code=202,
        summary="为已导出任务创建后台打标与过滤任务",
        response_model=TaggingJobResponse,
        response_model_exclude_unset=True,
    )
    async def create(job_id: str, query: TaggingQuery, request: Request) -> dict:
        return request.app.state.tagging.create(job_id, query)

    @router.get(
        "/v1/tagging/{tagging_id}",
        summary="查询打标进度与候选统计",
        response_model=TaggingJobResponse,
        response_model_exclude_unset=True,
    )
    async def status(tagging_id: str, request: Request) -> dict:
        return await asyncio.to_thread(request.app.state.tagging.get, tagging_id)

    @router.get(
        "/v1/tagging/{tagging_id}/results",
        summary="分页查看候选、排除与待复核结果",
        response_model=TaggingResultsPage,
        response_model_exclude_unset=True,
    )
    async def results(
        tagging_id: str,
        request: Request,
        candidate_level: Literal[
            "priority_candidate", "ordinary_candidate", "review", "excluded"
        ]
        | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        task_id: str = Query(default="", max_length=64, pattern=r"^[0-9]*$"),
    ) -> dict:
        index = await asyncio.to_thread(request.app.state.tagging.results, tagging_id)
        records = [
            r
            for r in index["records"]
            if (candidate_level is None or r["candidate_level"] == candidate_level)
            and task_id in r["task_id"]
        ]
        return {
            "tagging_id": tagging_id,
            "total": len(records),
            "offset": offset,
            "limit": limit,
            "records": records[offset : offset + limit],
        }

    @router.get(
        "/v1/tagging/{tagging_id}/tasks/{task_id}", summary="查看单任务标签和证据"
    )
    async def task_result(tagging_id: str, task_id: str, request: Request) -> dict:
        return await asyncio.to_thread(
            request.app.state.tagging.task_result, tagging_id, task_id
        )

    @router.get(
        "/v1/tagging/{tagging_id}/selected",
        summary="下载当前策略的候选清单",
        response_class=JSONResponse,
    )
    async def selected(tagging_id: str, request: Request) -> JSONResponse:
        manager = request.app.state.tagging
        index = await asyncio.to_thread(manager.results, tagging_id)
        return JSONResponse(
            [r for r in index["records"] if r["decision"] == "keep"],
            headers={
                "Content-Disposition": (
                    f'attachment; filename="flux_selected_{tagging_id}.json"'
                ),
                "Cache-Control": "no-store",
            },
        )

    @router.get(
        "/v1/tagging/{tagging_id}/download",
        summary="下载打标结果 ZIP，可按 task_id 批量选择任务",
        response_class=FileResponse,
    )
    async def download(
        tagging_id: str,
        request: Request,
        task_id: Annotated[
            list[str] | None,
            Query(description="默认完整下载；多选重复参数：task_id=111&task_id=222"),
        ] = None,
    ) -> FileResponse:
        manager = request.app.state.tagging
        path = await asyncio.to_thread(manager.download, tagging_id, task_id)
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"flux_tagging_{tagging_id}.zip",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @router.post(
        "/v1/tagging/{tagging_id}/cancel",
        summary="停止后续打标并保留已处理结果",
        response_model=TaggingJobResponse,
        response_model_exclude_unset=True,
    )
    async def cancel(tagging_id: str, request: Request) -> dict:
        return request.app.state.tagging.cancel(tagging_id)

    return router
