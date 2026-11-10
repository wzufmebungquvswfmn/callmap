"""HTTP application, lifecycle, admission control and error handling."""

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from flux_task_service.client import OpenAPIClient
from flux_task_service.config import Settings
from flux_task_service.data_api import data_router
from flux_task_service.errors import ServiceError
from flux_task_service.exports import ExportManager
from flux_task_service.models import (
    CollectQuery,
    CollectResult,
    OptionsQuery,
    OptionsResult,
    PageQuery,
    PageResult,
)
from flux_task_service.options import OptionsService
from flux_task_service.resources import ResourceService
from flux_task_service.service import TaskService
from flux_task_service.tagging.api import tagging_router
from flux_task_service.tagging.service import TaggingManager

logger = logging.getLogger("uvicorn.error")


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=config.request_timeout_seconds,
            limits=httpx.Limits(max_connections=config.max_concurrent_queries + 2),
        ) as http:
            app.state.service = TaskService(OpenAPIClient(config, http), config)
            app.state.options = OptionsService(app.state.service, config)
            app.state.admission = asyncio.Semaphore(config.max_concurrent_queries)
            app.state.resources = ResourceService(app.state.service.client, config)
            app.state.exports = ExportManager(
                app.state.resources, app.state.service, config, app.state.admission
            )
            app.state.tagging = TaggingManager(app.state.exports, config)
            try:
                yield
            finally:
                await app.state.tagging.close()
                await app.state.exports.close()

    app = FastAPI(
        title="Flux Task ID Service",
        version="0.1.0",
        lifespan=lifespan,
        description=(
            "Filter Flux tasks and export details, traces, metrics and artifacts."
        ),
    )
    web_dir = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")
    page = (web_dir / "index.html").read_text(encoding="utf-8")
    page = page.replace(
        "<!--UI_CONFIG-->",
        '<script type="application/json" id="ui-config">'
        + json.dumps(
            {
                "api_key_required": config.api_key is not None,
                "max_task_ids": config.max_task_ids,
            }
        )
        + "</script>",
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if config.api_key is not None:
            expected = config.api_key.get_secret_value()
            if (
                not expected
                or credentials is None
                or not secrets.compare_digest(
                    credentials.credentials.encode(), expected.encode()
                )
            ):
                raise ServiceError("UNAUTHORIZED", "Valid bearer API key required", 401)

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        logger.warning(
            "Service request failed: method=%s path=%s status=%s code=%s "
            "upstream_code=%s cause=%s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.code,
            exc.upstream_code,
            type(exc.__cause__).__name__ if exc.__cause__ else None,
        )
        error = {"code": exc.code, "message": exc.message}
        if exc.upstream_code is not None:
            error["upstream_code"] = exc.upstream_code
        if exc.fallback_errors is not None:
            error["fallback_errors"] = exc.fallback_errors
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code, content={"error": error}, headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Do not echo submitted values or request bodies in errors.
        details = [
            {"location": list(error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "INVALID_QUERY",
                    "message": "Invalid query",
                    "details": details,
                }
            },
        )

    @app.get("/health/live", tags=["健康检查"])
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["健康检查"])
    async def ready() -> dict[str, str]:
        try:
            config.read_secret()
        except (OSError, ValueError) as exc:
            raise ServiceError(
                "AUTH_NOT_CONFIGURED", "Service account secret is unavailable", 503
            ) from exc
        return {"status": "ready", "check": "credential_config"}

    @app.post(
        "/v1/filter-options",
        response_model=OptionsResult,
        dependencies=[Depends(authorize)],
        summary="获取筛选选项（分类支持级联）",
        description="在指定包含/排除范围内，从真实任务数据提取选项；不是全局分类字典。",
        tags=["筛选"],
    )
    async def filter_options(query: OptionsQuery, request: Request) -> OptionsResult:
        try:
            async with asyncio.timeout(config.options_query_timeout_seconds):
                async with request.app.state.admission:
                    return await request.app.state.options.get_options(query)
        except TimeoutError as exc:
            raise ServiceError(
                "QUERY_TIMEOUT", "Options query deadline exceeded", 504
            ) from exc

    @app.post(
        "/v1/task-ids/query",
        summary="按条件查询一页 task ID",
        tags=["筛选"],
        response_model=PageResult,
        dependencies=[Depends(authorize)],
    )
    async def query_page(query: PageQuery, request: Request) -> PageResult:
        try:
            async with asyncio.timeout(config.query_timeout_seconds):
                async with request.app.state.admission:
                    return await request.app.state.service.query_page(query)
        except TimeoutError as exc:
            raise ServiceError(
                "QUERY_TIMEOUT", "Task query deadline exceeded", 504
            ) from exc

    @app.post(
        "/v1/task-ids/collect",
        summary="按条件收集全部 task ID",
        tags=["筛选"],
        response_model=CollectResult,
        dependencies=[Depends(authorize)],
    )
    async def collect(query: CollectQuery, request: Request) -> CollectResult:
        try:
            async with asyncio.timeout(config.query_timeout_seconds):
                async with request.app.state.admission:
                    return await request.app.state.service.collect(query)
        except TimeoutError as exc:
            raise ServiceError(
                "QUERY_TIMEOUT", "Task query deadline exceeded", 504
            ) from exc

    app.include_router(data_router(config, authorize))
    app.include_router(tagging_router(authorize))
    return app
