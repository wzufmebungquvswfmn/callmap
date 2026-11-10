"""Task data, OpenAPI artifacts and OpenAPI-first trace/metrics retrieval."""

import asyncio
import base64
import hashlib
import json
import time
from pathlib import PurePosixPath

import httpx

from flux_task_service import metrics, token_usage
from flux_task_service.client import OpenAPIClient, bounded_post
from flux_task_service.config import Settings
from flux_task_service.conversation import fetch_conversation, projection_hides_content
from flux_task_service.errors import ServiceError
from flux_task_service.models import TaskIdentity, TraceQuery
from flux_task_service.raw_recovery import recover_raw_spans
from flux_task_service.trace import _span_list, normalize_fornax_span, render_tree


def safe_artifact_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        value.startswith("/")
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(path) != value
    ):
        raise ServiceError(
            "INVALID_ARTIFACT_PATH", "Use a normalized relative path", 422
        )
    return value


def public_error(exc: ServiceError) -> dict:
    result = {"code": exc.code, "message": exc.message}
    if exc.upstream_code is not None:
        result["upstream_code"] = exc.upstream_code
    return result


class FornaxClient:
    """Separate personal auth with optional desktop refresh and shared pacing."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http
        self.lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.auth_lock = asyncio.Lock()
        self.personal_token = ""
        self.rejected_token = ""
        self.configured_token = ""

    @staticmethod
    def expired(token: str) -> bool:
        try:
            payload = token.split(".")[1]
            data = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )
            expiration = data.get("exp")
            return (
                isinstance(expiration, (int, float)) and expiration < time.time() + 60
            )
        except (ValueError, IndexError, AttributeError, TypeError):
            return False

    @staticmethod
    def chrome_cookies() -> str:
        try:
            import browser_cookie3

            jar = browser_cookie3.chrome(domain_name="cloud.bytedance.net")
            return "; ".join(
                f"{cookie.name}={cookie.value}"
                for cookie in jar
                if "CAS_SESSION" in cookie.name.upper() or cookie.name == "csrf_token"
            )
        except Exception as exc:
            raise ServiceError(
                "FORNAX_REFRESH_FAILED",
                "Desktop refresh requires the desktop extra and a Chrome SSO login",
                503,
            ) from exc

    async def auth_token(
        self,
        rejected: str | None = None,
        *,
        reload: bool = False,
        refresh: bool = False,
    ) -> str:
        async with self.auth_lock:
            try:
                configured = self.token()
            except ServiceError:
                configured = ""
                if not (
                    self.personal_token
                    or self.settings.fornax_refresh_from_chrome
                    or refresh
                ):
                    raise
            if configured != self.configured_token or reload:
                self.personal_token = ""
                self.configured_token = configured
            token = self.personal_token or configured
            rejected_tokens = {rejected, self.rejected_token}
            if (
                not refresh
                and token
                and token not in rejected_tokens
                and not self.expired(token)
            ):
                return token
            if not (self.settings.fornax_refresh_from_chrome or refresh):
                if token and self.expired(token):
                    raise ServiceError(
                        "FORNAX_TOKEN_EXPIRED",
                        "Fornax personal JWT expired; "
                        "update credentials or explicitly refresh",
                        503,
                    )
                raise ServiceError(
                    "FORNAX_UNAUTHORIZED" if token else "FORNAX_AUTH_NOT_CONFIGURED",
                    "Fornax personal JWT rejected or unavailable; "
                    "update credentials or refresh",
                    503,
                )
            cookies = await asyncio.to_thread(self.chrome_cookies)
            if not cookies:
                raise ServiceError(
                    "FORNAX_REFRESH_FAILED", "No Chrome SSO session available", 503
                )
            try:
                response = await self.http.get(
                    self.settings.iam_url,
                    headers={"Cookie": cookies, "accept": "application/json"},
                    timeout=self.settings.request_timeout_seconds,
                )
            except httpx.RequestError as exc:
                raise ServiceError(
                    "FORNAX_REFRESH_FAILED", "Personal JWT refresh failed", 503
                ) from exc
            token = response.headers.get("X-Jwt-Token", "").strip()
            if (
                response.status_code != 200
                or not token
                or token in rejected_tokens
                or self.expired(token)
            ):
                raise ServiceError(
                    "FORNAX_REFRESH_FAILED", "IAM did not issue a personal JWT", 503
                )
            self.personal_token = token
            return token

    async def update_token(self, token: str) -> None:
        """Install an explicit in-memory credential without writing secrets to disk."""
        token = token.strip()
        if not token or self.expired(token):
            raise ServiceError(
                "FORNAX_TOKEN_EXPIRED", "Provided personal JWT is empty or expired", 422
            )
        async with self.auth_lock:
            try:
                self.configured_token = self.token()
            except ServiceError:
                self.configured_token = ""
            if token == self.rejected_token:
                raise ServiceError(
                    "FORNAX_UNAUTHORIZED", "Provided JWT was already rejected", 503
                )
            self.personal_token = token

    def token(self) -> str:
        try:
            if self.settings.fornax_jwt is not None:
                value = self.settings.fornax_jwt.get_secret_value().strip()
            elif self.settings.fornax_jwt_file is not None:
                value = self.settings.fornax_jwt_file.read_text().strip()
            else:
                value = ""
        except (OSError, UnicodeError) as exc:
            raise ServiceError(
                "FORNAX_AUTH_NOT_CONFIGURED", "Fornax JWT unavailable", 503
            ) from exc
        if not value:
            raise ServiceError(
                "FORNAX_AUTH_NOT_CONFIGURED", "Configure a Fornax personal JWT", 503
            )
        return value

    async def post(self, body: dict) -> dict:
        token = await self.auth_token()
        refreshed = False
        retries = 0
        while True:
            # Hold the gate through the request as well as pacing, like the old script.
            async with self.lock:
                if token == self.rejected_token:
                    token = await self.auth_token(rejected=token)
                delay = self.next_request_at - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self.next_request_at = (
                    time.monotonic() + self.settings.fornax_min_interval_seconds
                )
                try:
                    response = await bounded_post(
                        self.http,
                        f"{self.settings.fornax_base_url}/open-api/observability/spans/search",
                        body,
                        {"X-Jwt-Token": token},
                        self.settings.max_download_bytes,
                        self.settings.request_timeout_seconds,
                    )
                except httpx.RequestError:
                    response = None
            payload = None
            if response is not None:
                try:
                    payload = response.json()
                except ValueError:
                    pass
            code = payload.get("code") if isinstance(payload, dict) else None
            if response is not None and response.status_code == 401:
                self.rejected_token = token
                if not refreshed:
                    new_token = await self.auth_token(rejected=token)
                    if not new_token or new_token == token:
                        raise ServiceError(
                            "FORNAX_UNAUTHORIZED", "Fornax JWT rejected", 503
                        )
                    token = new_token
                    refreshed = True
                    continue
                raise ServiceError("FORNAX_UNAUTHORIZED", "Fornax JWT rejected", 503)
            if response is not None and response.is_success and code in (None, 0):
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    return payload["data"]
                raise ServiceError(
                    "FORNAX_PROTOCOL_ERROR", "Fornax response lacks data"
                )
            retryable = (
                response is None
                or response.status_code in {408, 429, 500, 502, 503, 504}
                or code == 600900205
            )
            if retryable and retries < self.settings.max_retries:
                await asyncio.sleep(min(2 ** (retries + 1), 8))
                retries += 1
                continue
            raise ServiceError("FORNAX_UNAVAILABLE", "Fornax query failed", 503)

    async def spans(self, query: TraceQuery) -> dict:
        end = query.end_ms if query.end_ms is not None else int(time.time() * 1000)
        start = (
            query.start_ms
            if query.start_ms is not None
            else end - self.settings.fornax_range_days * 86400000
        )
        if start > end:
            raise ServiceError("INVALID_QUERY", "start_ms exceeds end_ms", 422)
        seen = {}
        cursors = set()
        cursor = None
        for _ in range(self.settings.max_pages):
            body = {
                "platform_type": self.settings.fornax_platform,
                "span_list_type": "all_span",
                "start_time": start,
                "end_time": end,
                "workspace_id": self.settings.fornax_workspace_id,
                "page_size": 100,
                "filters": {
                    "query_and_or": "and",
                    "filter_fields": [
                        {
                            "field_name": "thread_id",
                            "field_type": "string",
                            "query_type": "eq",
                            "values": [query.task_id],
                        }
                    ],
                },
                "order_bys": [{"field": "start_time", "is_asc": True}],
            }
            if cursor:
                body["page_token"] = cursor
            try:
                data = await self.post(body)
                spans, error = _span_list(data.get("spans"))
                if error or not isinstance(data.get("has_more"), bool):
                    raise ServiceError(
                        "FORNAX_PROTOCOL_ERROR", "Invalid Fornax spans or pagination"
                    )
            except ServiceError as exc:
                if not seen:
                    raise
                return {
                    "spans": list(seen.values()),
                    "complete": False,
                    "error": public_error(exc),
                    "start_ms": start,
                    "end_ms": end,
                }
            for span in spans:
                seen[(str(span.get("trace_id") or ""), str(span["span_id"]))] = span
            if not data["has_more"]:
                return {
                    "spans": list(seen.values()),
                    "complete": True,
                    "start_ms": start,
                    "end_ms": end,
                }
            cursor = data.get("next_page_token")
            if (
                not isinstance(cursor, str)
                or not cursor
                or cursor in cursors
                or not spans
            ):
                break
            cursors.add(cursor)
        return {
            "spans": list(seen.values()),
            "complete": False,
            "error": {
                "code": "FORNAX_PAGINATION_INCOMPLETE",
                "message": "Fornax cursor invalid or page limit reached",
            },
            "start_ms": start,
            "end_ms": end,
        }


class ResourceService:
    def __init__(self, api: OpenAPIClient, settings: Settings):
        self.api = api
        self.settings = settings
        self.fornax = FornaxClient(settings, api.http)

    async def task(self, task_id: str) -> dict:
        data = await self.api.query_tasks({"task_id_list": [task_id], "limit": 50})
        items = data.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise ServiceError("UPSTREAM_PROTOCOL_ERROR", "Invalid task details")
        if any(
            item.get("id_kind") != "task" or item.get("task_id") != task_id
            for item in items
        ):
            raise ServiceError(
                "UPSTREAM_FILTER_MISMATCH", "Unexpected task detail identity"
            )
        if not items:
            raise ServiceError("TASK_NOT_FOUND", "No task detail found", 404)
        page_info = data.get("page_info")
        if not isinstance(page_info, dict) or not isinstance(
            page_info.get("has_more"), bool
        ):
            raise ServiceError("UPSTREAM_PROTOCOL_ERROR", "Invalid task pagination")
        if page_info["has_more"]:
            raise ServiceError(
                "UPSTREAM_PAGINATION_ERROR",
                "Single task details unexpectedly paginated",
            )
        return data

    async def openapi_trace(
        self, task_id: str, *, include_raw_spans: bool = False
    ) -> dict:
        return await self.api.request_json(
            "/trace/query",
            {
                "task_id": task_id,
                "include_raw_spans": include_raw_spans,
                "compress": True,
            },
            headers={"X-Flux-Trace-Contract": "2"},
        )

    async def trace_and_metrics(
        self,
        query: TraceQuery,
        *,
        want_trace=True,
        want_metrics=True,
        include_conversation=False,
        task_detail: dict | None = None,
    ) -> dict:
        result = {}
        errors = {}
        payload = None
        detail_candidates = [task_detail] if task_detail is not None else []
        raw_requested = want_trace and query.include_raw_spans
        if query.source != "fornax":
            try:
                try:
                    payload = await self.openapi_trace(
                        query.task_id, include_raw_spans=raw_requested
                    )
                except ServiceError as exc:
                    if not raw_requested or (
                        exc.code != "DOWNLOAD_LIMIT_EXCEEDED"
                        and exc.upstream_code != 1005
                    ):
                        raise
                    errors["openapi_raw"] = public_error(exc)
                    payload = await self.api.request_json(
                        "/trace/query",
                        {
                            "task_id": query.task_id,
                            "include_raw_spans": False,
                            "compress": True,
                        },
                        headers={"X-Flux-Trace-Contract": "2"},
                    )
                result["openapi"] = payload
                meta = payload.get("meta") or {}
                raw = payload.get("raw_spans")
                sessions = payload.get("sessions")
                complete = token_usage.execution_complete(meta)
                if want_trace and (
                    (isinstance(raw, list) and raw)
                    or (isinstance(sessions, list) and sessions)
                ):
                    if raw is not None and (
                        not isinstance(raw, list)
                        or any(not isinstance(span, dict) for span in raw)
                    ):
                        raise ServiceError(
                            "UPSTREAM_PROTOCOL_ERROR", "Invalid OpenAPI raw spans"
                        )
                    expected = (meta.get("counts") or {}).get("spans")
                    raw_complete = (
                        complete is True
                        and isinstance(expected, int)
                        and not isinstance(expected, bool)
                        and isinstance(raw, list)
                        and len(raw) == expected
                        and expected > 0
                    )
                    result["trace"] = {
                        "source": "flux_insight_openapi",
                        "complete": complete,
                        "format": payload.get("schema_version", "openapi"),
                        "data": payload,
                        "spans": raw or [],
                        "raw_spans_requested": raw_requested,
                        "raw_spans_complete": raw_complete if raw_requested else None,
                        "raw_spans_status": "not_requested"
                        if not raw_requested
                        else "complete"
                        if raw_complete
                        else "partial"
                        if raw
                        else "unavailable",
                        "warnings": []
                        if not raw_requested or raw_complete
                        else ["原始 spans 未确认完整，保留 OpenAPI read-model"],
                    }
                    if projection_hides_content(meta):
                        result["trace"]["warnings"].append(
                            "上游仅返回执行元数据，已省略会话正文和步骤 payload"
                        )
                    if "openapi_raw" in errors:
                        result["trace"]["warnings"].append(
                            "原始 spans 超过响应或查询上限，已改用 OpenAPI read-model"
                        )
                elif want_trace:
                    errors["trace"] = {
                        "code": "TRACE_NOT_FOUND",
                        "message": "OpenAPI returned no trace data",
                    }
                if want_metrics:
                    calls, intervals, count, first, error, timing_coverage = (
                        metrics._norm_insight_openapi({"data": payload})
                    )
                    if error:
                        errors["metrics"] = {
                            "code": "METRICS_NOT_FOUND",
                            "message": error,
                        }
                    else:
                        values = metrics.build_metrics(
                            calls, intervals, model_call_count=count, first_resp=first
                        )
                        if count is None:
                            values["model_call_count"] = None
                        warnings = []
                        if not intervals:
                            for field in (
                                "end_to_end_latency_ms",
                                "total_latency_ms",
                                "interaction_latency_ms",
                            ):
                                values[field] = None
                            warnings.append("没有可用的主会话时间区间")
                        result["metrics"] = {
                            "source": "flux_insight_openapi",
                            "complete": complete,
                            "execution_complete": complete,
                            "values": values,
                            "warnings": warnings,
                        }
                        metrics.apply_timing(result["metrics"], timing_coverage)
                        token_usage.apply_tokens(
                            result["metrics"], token_usage.summary_tokens(payload)
                        )
            except (TypeError, ValueError, AttributeError) as exc:
                errors["openapi"] = {
                    "code": "UPSTREAM_PROTOCOL_ERROR",
                    "message": "Invalid OpenAPI trace read-model",
                }
                if query.source == "openapi":
                    raise ServiceError(
                        "UPSTREAM_PROTOCOL_ERROR", "Invalid OpenAPI trace read-model"
                    ) from exc
            except ServiceError as exc:
                errors["openapi"] = public_error(exc)
        if (
            query.recover_raw_spans
            and "openapi_raw" in errors
            and "trace" in result
            and payload is not None
        ):
            spans, recovery = await recover_raw_spans(self.api, query.task_id, payload)
            trace = result["trace"]
            trace["raw_recovery"] = recovery
            trace["spans"] = spans
            trace["raw_spans_complete"] = recovery["complete"]
            trace["raw_spans_status"] = (
                "complete"
                if recovery["complete"]
                else "partial"
                if spans
                else "unavailable"
            )
            if recovery["complete"]:
                trace["warnings"] = [
                    warning
                    for warning in trace["warnings"]
                    if warning
                    not in {
                        "OpenAPI 未返回原始 spans，保留 read-model",
                        "原始 spans 未确认完整，保留 OpenAPI read-model",
                        "原始 spans 超过响应或查询上限，已改用 OpenAPI read-model",
                    }
                ]
            else:
                trace["warnings"].append(
                    f"按会话补取原始 spans 未完成："
                    f"{recovery['recovered_spans']}/{recovery['expected_spans']}"
                )
        need_trace = want_trace and "trace" not in result
        need_metrics = want_metrics and "metrics" not in result
        if query.source != "openapi" and (need_trace or need_metrics):
            try:
                raw = await self.fornax.spans(query)
                result["fornax"] = raw
                if not raw["spans"]:
                    raise ServiceError(
                        "TRACE_NOT_FOUND", "Fornax returned no spans", 404
                    )
                if "error" in raw:
                    errors["fornax"] = raw["error"]
                spans = [normalize_fornax_span(span) for span in raw["spans"]]
                if need_trace:
                    result["trace"] = {
                        "source": "fornax",
                        "complete": raw["complete"],
                        "format": "flux_insight_spans",
                        "spans": spans,
                        "raw_spans_complete": raw["complete"]
                        if raw_requested
                        else None,
                        "raw_spans_requested": raw_requested,
                        "raw_spans_status": "not_requested"
                        if not raw_requested
                        else "complete"
                        if raw["complete"]
                        else "partial",
                        "tree": render_tree(spans),
                        "warnings": ["Fornax 完整性仅针对指定时间窗口"],
                        "window": {
                            "start_ms": raw["start_ms"],
                            "end_ms": raw["end_ms"],
                        },
                    }
                    if "error" in raw:
                        result["trace"]["warnings"].append(raw["error"]["message"])
                if need_metrics:
                    calls = [
                        span
                        for span in raw["spans"]
                        if str(span.get("span_name", "")).split("(", 1)[0]
                        == "model-call"
                    ]
                    turns = [
                        span
                        for span in raw["spans"]
                        if str(span.get("span_name", "")).startswith("agent-turn")
                    ]
                    normalized, intervals, timing_coverage = metrics._norm_fornax(
                        calls, turns
                    )
                    if not normalized and not intervals:
                        raise ServiceError(
                            "METRICS_NOT_FOUND",
                            "Fornax has no model calls or primary turns",
                            404,
                        )
                    values = metrics.build_metrics(normalized, intervals)
                    if not intervals:
                        for field in (
                            "end_to_end_latency_ms",
                            "total_latency_ms",
                            "interaction_latency_ms",
                        ):
                            values[field] = None
                    result["metrics"] = {
                        "source": "fornax",
                        "complete": raw["complete"],
                        "execution_complete": raw["complete"],
                        "values": values,
                        "warnings": ["Fornax 完整性仅针对指定时间窗口"],
                        "window": {
                            "start_ms": raw["start_ms"],
                            "end_ms": raw["end_ms"],
                        },
                    }
                    metrics.apply_timing(result["metrics"], timing_coverage)
                    token_usage.apply_tokens(
                        result["metrics"],
                        self.fornax_token_totals(raw),
                    )
            except ServiceError as exc:
                errors["fornax"] = public_error(exc)
        if include_conversation:
            if query.source != "fornax":
                conversation = await fetch_conversation(
                    self.api, query.task_id, query.conversation_mode
                )
            else:
                conversation = {
                    "status": "not_verified",
                    "complete": None,
                    "content_available": False,
                    "documents": [],
                    "errors": [],
                }
            result["conversation"] = conversation
            if (
                want_trace
                and "trace" not in result
                and conversation["content_available"]
            ):
                result["trace"] = {
                    "source": "flux_insight_openapi",
                    "complete": None,
                    "format": "conversation_only",
                    "spans": [],
                    "raw_spans_requested": raw_requested,
                    "raw_spans_complete": False if raw_requested else None,
                    "raw_spans_status": "unavailable"
                    if raw_requested
                    else "not_requested",
                    "warnings": ["执行 Trace 不可用，已保留会话正文"],
                }
            if "trace" in result:
                trace = result["trace"]
                trace["execution_complete"] = trace["complete"]
                trace["conversation_complete"] = conversation["complete"]
                trace["conversation"] = conversation
                trace["completeness_scope"] = {
                    "execution": "source-query-scope",
                    "conversation": conversation.get("completeness_scope"),
                }
                trace["complete"] = (
                    False
                    if False in (trace["execution_complete"], conversation["complete"])
                    else True
                    if trace["execution_complete"] is True
                    and conversation["complete"] is True
                    else None
                )
                if conversation["complete"] is not True:
                    trace["warnings"].append(
                        "未取得已确认完整的会话正文；执行结构或 spans 不代表完整会话"
                    )
        metric_result = result.get("metrics")
        if metric_result and metric_result["source"] == "flux_insight_openapi":
            conversation = result.get("conversation") or {}
            if conversation.get("errors"):
                errors["token_view"] = {
                    "code": "TOKEN_VIEW_INCOMPLETE",
                    "message": "Trace view read has errors",
                    "errors": conversation["errors"],
                }
            for document in conversation.get("documents") or []:
                detail_candidates.append(document["data"])
                normalized = token_usage.view_tokens(document["data"], query.task_id)
                if normalized is not None:
                    token_usage.apply_tokens(metric_result, normalized)
                    break
            if (
                not include_conversation
                and metric_result["token_evidence"]["complete"] is not True
            ):
                try:
                    async with asyncio.timeout(self.settings.request_timeout_seconds):
                        data = await self.api.request_json(
                            "/task",
                            {
                                "task_id_list": [query.task_id],
                                "limit": 50,
                                "include_trace": True,
                            },
                            max_retries=0,
                        )
                    detail_candidates.append(data)
                    normalized = token_usage.view_tokens(data, query.task_id)
                    if normalized is not None:
                        token_usage.apply_tokens(metric_result, normalized)
                except ServiceError as exc:
                    errors["token_view"] = public_error(exc)
                except TimeoutError:
                    errors["token_view"] = {
                        "code": "TOKEN_VIEW_TIMEOUT",
                        "message": "Additional Token view read deadline exceeded",
                    }
        # Timing alone must not suppress independent Token recovery.
        if want_metrics and (
            metric_result is None or metric_result["values"].get("total_tokens") is None
        ):
            if query.source != "openapi" and (
                "fornax" in result or "fornax" not in errors
            ):
                try:
                    raw = result.get("fornax")
                    if raw is None:
                        async with asyncio.timeout(
                            self.settings.request_timeout_seconds
                        ):
                            raw = await self.fornax.spans(query)
                        result["fornax"] = raw
                    if "error" in raw:
                        errors["fornax"] = raw["error"]
                    elif not raw["spans"]:
                        errors["fornax"] = {
                            "code": "TRACE_NOT_FOUND",
                            "message": "Fornax returned no spans",
                        }
                    if (
                        metric_result is not None
                        and metric_result["source"] != "fornax"
                    ):
                        token_usage.apply_tokens(
                            metric_result, self.fornax_token_totals(raw)
                        )
                except ServiceError as exc:
                    errors["fornax"] = public_error(exc)
                except TimeoutError:
                    errors["fornax"] = {
                        "code": "FORNAX_TOKEN_TIMEOUT",
                        "message": "Additional Token retrieval deadline exceeded",
                    }
            # Reuse the saved detail or Trace view before a bounded detail-only read.
            if query.source != "fornax" and (
                metric_result is None
                or metric_result["values"].get("total_tokens") is None
            ):
                if not detail_candidates:
                    try:
                        async with asyncio.timeout(
                            self.settings.request_timeout_seconds
                        ):
                            data = await self.api.request_json(
                                "/task",
                                {"task_id_list": [query.task_id], "limit": 50},
                                max_retries=0,
                            )
                        detail_candidates.append(data)
                    except ServiceError as exc:
                        errors["token_task_detail"] = public_error(exc)
                    except TimeoutError:
                        errors["token_task_detail"] = {
                            "code": "TOKEN_TASK_DETAIL_TIMEOUT",
                            "message": "Additional task detail read deadline exceeded",
                        }
                for data in detail_candidates:
                    normalized = token_usage.task_tokens(data, query.task_id)
                    if normalized is None:
                        continue
                    if metric_result is None:
                        metric_result = {
                            "source": "task_detail",
                            "execution_complete": None,
                            "values": metrics.build_metrics([], []),
                            "warnings": [],
                        }
                        metric_result["values"]["model_call_count"] = None
                        metrics.apply_timing(
                            metric_result,
                            {
                                "primary_turns": 0,
                                "known_primary_turns": 0,
                                "counts_consistent": False,
                            },
                        )
                        result["metrics"] = metric_result
                    token_usage.apply_tokens(metric_result, normalized)
                    break
        if metric_result is not None:
            metric_result["fallback_errors"] = dict(errors)
            if errors:
                metric_result["complete"] = False
                metric_result["warnings"].append(
                    "上游读取存在错误，详见 fallback_errors"
                )
        result["errors"] = errors
        return result

    @staticmethod
    def fornax_token_totals(raw: dict) -> dict:
        calls = [
            span
            for span in raw["spans"]
            if str(span.get("span_name", "")).split("(", 1)[0] == "model-call"
        ]
        normalized = token_usage.fornax_tokens(calls, raw["complete"])
        normalized["token_scope"] = {
            "kind": "fornax_window",
            "start_ms": raw["start_ms"],
            "end_ms": raw["end_ms"],
            "pagination_complete": raw["complete"],
        }
        if "error" in raw:
            normalized["fallback_errors"] = {"fornax": raw["error"]}
        return normalized

    async def artifacts(
        self, identity: TaskIdentity, *, max_retries: int | None = None
    ) -> dict:
        data = await self.api.request_json(
            "/task/changed/artifacts",
            identity.model_dump(exclude_none=True),
            max_retries=max_retries,
        )
        files = data.get("files")
        if not isinstance(files, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("path"), str)
            for item in files
        ):
            raise ServiceError(
                "UPSTREAM_PROTOCOL_ERROR", "Artifact manifest lacks files"
            )
        if isinstance(data.get("file_count"), bool) or data.get("file_count") != len(
            files
        ):
            raise ServiceError(
                "UPSTREAM_PROTOCOL_ERROR", "Artifact manifest count mismatch"
            )
        if len({item["path"] for item in files}) != len(files):
            raise ServiceError("UPSTREAM_PROTOCOL_ERROR", "Duplicate artifact paths")
        for item in files:
            safe_artifact_path(item["path"])
        if (
            identity.worker_session_id
            and (data.get("worker_session_id") or data.get("session_id"))
            != identity.worker_session_id
        ):
            raise ServiceError("UPSTREAM_FILTER_MISMATCH", "Artifact session mismatch")
        if (
            identity.task_id
            and data.get("task_id", identity.task_id) != identity.task_id
        ):
            raise ServiceError("UPSTREAM_FILTER_MISMATCH", "Artifact task mismatch")
        return data

    async def artifact(
        self, identity: TaskIdentity, path: str, *, max_retries: int | None = None
    ) -> httpx.Response:
        safe_artifact_path(path)
        response = await self.api.request_bytes(
            "/task/artifacts/raw",
            {**identity.model_dump(exclude_none=True), "path": path},
            max_retries=max_retries,
        )
        for field, header in (
            (identity.worker_session_id, "X-Artifact-Worker-Session-ID"),
            (identity.task_id, "X-Artifact-Task-ID"),
        ):
            if field and response.headers.get(header, field) != field:
                raise ServiceError(
                    "UPSTREAM_FILTER_MISMATCH", "Artifact identity mismatch"
                )
        if response.headers.get("X-Artifact-Path", path) != path:
            raise ServiceError("UPSTREAM_FILTER_MISMATCH", "Artifact path mismatch")
        digest = response.headers.get("X-Artifact-SHA256")
        if (
            digest
            and hashlib.sha256(response.content).hexdigest().lower() != digest.lower()
        ):
            raise ServiceError("ARTIFACT_INTEGRITY_ERROR", "Artifact checksum mismatch")
        size = response.headers.get("X-Artifact-Size")
        if size is not None and (
            not size.isdecimal() or int(size) != len(response.content)
        ):
            raise ServiceError("ARTIFACT_INTEGRITY_ERROR", "Artifact size mismatch")
        return response
