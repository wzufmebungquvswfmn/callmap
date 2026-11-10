"""Async OpenAPI transport with service-account authentication and pacing."""

import asyncio
import base64
import json
import time

import httpx

from flux_task_service.config import Settings
from flux_task_service.errors import ServiceError


async def bounded_post(
    http: httpx.AsyncClient,
    url: str,
    body: dict,
    headers: dict,
    byte_limit: int,
    request_timeout_seconds: float,
) -> httpx.Response:
    """Bound decoded response bytes for JSON, compressed traces and files."""
    async with http.stream(
        "POST", url, headers=headers, json=body, timeout=request_timeout_seconds
    ) as stream:
        chunks = bytearray()
        async for chunk in stream.aiter_bytes():
            if len(chunks) + len(chunk) > byte_limit:
                raise ServiceError(
                    "DOWNLOAD_LIMIT_EXCEEDED",
                    "Upstream response exceeded the download byte limit",
                    422,
                )
            chunks.extend(chunk)
        # Preserve raw header bytes, including UTF-8 artifact paths. A dict of
        # decoded strings would make httpx re-encode all header values as ASCII.
        response_headers = stream.headers.copy()
        response_headers.pop("content-encoding", None)
        response_headers.pop("content-length", None)
        return httpx.Response(
            stream.status_code, headers=response_headers, content=bytes(chunks)
        )


class ServiceAccountAuth:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self, rejected_token: str | None = None) -> str:
        async with self._lock:
            if (
                self._token
                and time.time() < self._expires_at - 60
                and self._token != rejected_token
            ):
                return self._token
            try:
                secret = self.settings.read_secret()
            except (OSError, ValueError) as exc:
                raise ServiceError(
                    "AUTH_NOT_CONFIGURED", "Service account secret is unavailable", 503
                ) from exc
            for attempt in range(self.settings.max_retries + 1):
                try:
                    response = await self.http.get(
                        self.settings.iam_url,
                        headers={"Authorization": f"Bearer {secret}"},
                        timeout=self.settings.request_timeout_seconds,
                    )
                except httpx.RequestError as exc:
                    if attempt < self.settings.max_retries:
                        await OpenAPIClient._backoff(attempt)
                        continue
                    raise ServiceError(
                        "IAM_UNAVAILABLE", "Cannot obtain service account JWT", 503
                    ) from exc
                if response.status_code in {408, 429, 500, 502, 503, 504}:
                    if attempt < self.settings.max_retries:
                        await OpenAPIClient._backoff(
                            attempt, response.headers.get("Retry-After")
                        )
                        continue
                break
            token = response.headers.get("X-Jwt-Token", "").strip()
            if response.status_code != 200 or not token:
                raise ServiceError(
                    "IAM_AUTH_FAILED", "IAM did not issue a service account JWT", 503
                )
            self._token = token
            self._expires_at = self._expiry(token)
            return token

    @staticmethod
    def _expiry(token: str) -> float:
        # exp is used only for cache freshness, not for identity verification.
        # IAM over HTTPS is the credential source; opaque tokens get a short TTL.
        try:
            payload = token.split(".")[1]
            decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            expiration = json.loads(decoded).get("exp")
            if isinstance(expiration, (int, float)) and expiration > time.time():
                return float(expiration)
        except (IndexError, ValueError, AttributeError, TypeError):
            pass
        return time.time() + 300


class OpenAPIClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http = http
        self.auth = ServiceAccountAuth(settings, http)
        self._pace_lock = asyncio.Lock()
        self._next_request_at = 0.0

    async def _pace(self) -> None:
        async with self._pace_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = (
                time.monotonic() + 1 / self.settings.requests_per_second
            )

    async def query_tasks(self, body: dict) -> dict:
        return await self.request_json("/task", body)

    async def request_json(
        self,
        path: str,
        body: dict,
        *,
        headers: dict | None = None,
        max_retries: int | None = None,
        max_response_bytes: int | None = None,
    ) -> dict:
        result = await self._request(
            path,
            body,
            headers=headers,
            max_retries=max_retries,
            max_response_bytes=max_response_bytes,
        )
        return result

    async def request_bytes(
        self, path: str, body: dict, *, max_retries: int | None = None
    ) -> httpx.Response:
        return await self._request(path, body, raw=True, max_retries=max_retries)

    async def _request(
        self,
        path: str,
        body: dict,
        *,
        raw: bool = False,
        headers: dict | None = None,
        max_retries: int | None = None,
        max_response_bytes: int | None = None,
    ) -> dict | httpx.Response:
        retry_limit = self.settings.max_retries if max_retries is None else max_retries
        token = await self.auth.get_token()
        refreshed = False
        retries = 0
        while True:
            await self._pace()
            try:
                response = await bounded_post(
                    self.http,
                    f"{self.settings.base_url}/openapi/v1{path}",
                    body,
                    {**(headers or {}), "X-Jwt-Token": token},
                    min(
                        self.settings.max_download_bytes,
                        max_response_bytes
                        if max_response_bytes is not None
                        else self.settings.max_download_bytes,
                    ),
                    self.settings.request_timeout_seconds,
                )
            except httpx.RequestError as exc:
                if retries < retry_limit:
                    await self._backoff(retries)
                    retries += 1
                    continue
                raise ServiceError(
                    "UPSTREAM_UNAVAILABLE", "Flux OpenAPI request failed", 503
                ) from exc

            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            if isinstance(code, bool) or not isinstance(code, int):
                code = None
            data = payload.get("data") if isinstance(payload, dict) else None
            detail = data.get("error") if isinstance(data, dict) else None
            detail = detail if isinstance(detail, dict) else {}
            reason = detail.get("code")
            authz_error = isinstance(reason, str) and reason.startswith("AUTHZ_")

            if raw and response.is_success:
                error_envelope = (
                    isinstance(payload, dict)
                    and code is not None
                    and code != 0
                    and ("msg" in payload or code in {2001, 2002, 2003} or authz_error)
                    and "data" in payload
                    and "x-artifact-path" not in response.headers
                )
                if not error_envelope:
                    return response
            if response.is_success and code == 0:
                if not isinstance(data, dict):
                    raise ServiceError("UPSTREAM_PROTOCOL_ERROR", "Invalid task data")
                return data
            if not authz_error and (response.status_code == 401 or code == 2001):
                if not refreshed:
                    token = await self.auth.get_token(rejected_token=token)
                    refreshed = True
                    continue
                raise ServiceError(
                    "UPSTREAM_UNAUTHORIZED",
                    "Service account JWT was rejected",
                    503,
                    upstream_code=code,
                )
            if response.status_code == 403 or code == 2002 or authz_error:
                if authz_error and detail.get("retriable") is True:
                    retryable = True
                else:
                    raise ServiceError(
                        "UPSTREAM_FORBIDDEN",
                        "Service account access was denied",
                        502,
                        upstream_code=code,
                    )
            else:
                # Recognize only known raw-artifact reasons; never expose the
                # arbitrary upstream message or mistake permission errors for them.
                message = payload.get("msg") if isinstance(payload, dict) else None
                if path == "/task/artifacts/raw" and code == 3001:
                    if isinstance(message, str):
                        if "产物内容与 manifest 的 size/sha256 不一致" in message:
                            raise ServiceError(
                                "ARTIFACT_INTEGRITY_ERROR",
                                "Artifact content does not match upstream manifest",
                                502,
                                upstream_code=code,
                            )
                        if "下载 worker 产物失败" in message:
                            raise ServiceError(
                                "ARTIFACT_DOWNLOAD_FAILED",
                                "Upstream could not download the worker artifact",
                                502,
                                upstream_code=code,
                            )
                retryable = response.status_code in {
                    408,
                    429,
                    500,
                    502,
                    503,
                    504,
                } or code in {2003, 3001, 3006, 9000}
            if retryable and retries < retry_limit:
                await self._backoff(retries, response.headers.get("Retry-After"))
                retries += 1
                continue
            if retryable:
                raise ServiceError(
                    "UPSTREAM_RETRIES_EXHAUSTED",
                    "Flux OpenAPI retries exhausted",
                    503,
                    upstream_code=code,
                )
            if code is None:
                raise ServiceError(
                    "UPSTREAM_PROTOCOL_ERROR", "Invalid Flux OpenAPI response"
                )
            raise ServiceError(
                "UPSTREAM_REJECTED",
                "Flux OpenAPI rejected the query",
                502,
                upstream_code=code,
            )

    @staticmethod
    async def _backoff(attempt: int, retry_after: str | None = None) -> None:
        delay = min(2**attempt, 8)
        if retry_after is not None:
            try:
                delay = max(delay, min(max(float(retry_after), 0), 10))
            except ValueError:
                pass
        await asyncio.sleep(delay)
