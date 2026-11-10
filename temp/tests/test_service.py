"""Exercise HTTP behavior against a deterministic mock IAM and Flux OpenAPI."""

import asyncio
import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.client import OpenAPIClient, ServiceAccountAuth
from flux_task_service.config import Settings

FILTERS = {
    "query_and_or": "and",
    "filter_fields": [
        {
            "field_name": "created_by",
            "field_type": "string",
            "query_type": "in",
            "values": ["alice"],
        }
    ],
}


def settings(**overrides) -> Settings:
    defaults = {
        "service_secret": SecretStr("test-service-secret"),
        "max_retries": 0,
        "requests_per_second": 10,
    }
    return Settings(_env_file=None, **(defaults | overrides))


def page(ids=(), *, cursor=None, has_more=False, sessions=()) -> httpx.Response:
    items = [{"id_kind": "task", "task_id": task_id} for task_id in ids]
    items += [{"id_kind": "session", "task_id": value} for value in sessions]
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "items": items,
                "page_info": {
                    "has_more": has_more,
                    "next_cursor": cursor,
                },
            },
        },
    )


def transport_for(pages, requests=None):
    iterator = iter(pages)

    def handle(request):
        if request.url.path == "/auth/api/v1/jwt":
            assert request.headers["Authorization"] == "Bearer test-service-secret"
            return httpx.Response(200, headers={"X-Jwt-Token": "service-jwt"})
        assert request.url.path == "/openapi/v1/task"
        assert request.headers["X-Jwt-Token"] == "service-jwt"
        if requests is not None:
            requests.append(json.loads(request.content))
        return next(iterator)

    return httpx.MockTransport(handle)


def test_collect_preserves_filter_and_cursor_deduplicates_and_skips_sessions():
    requests = []
    transport = transport_for(
        [
            page(["111", "222"], cursor="c1", has_more=True, sessions=["999"]),
            page(["222", "333"]),
        ],
        requests,
    )
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": FILTERS})
    assert response.status_code == 200
    assert response.json() == {
        "task_ids": ["111", "222", "333"],
        "count": 3,
        "scanned_count": 5,
        "skipped_session_count": 1,
        "page_count": 2,
        "exhausted": True,
        "excluded_count": 0,
    }
    assert requests == [
        {"filters": FILTERS, "limit": 200},
        {"filters": FILTERS, "limit": 200, "cursor": "c1"},
    ]


def test_session_only_page_still_returns_next_cursor():
    transport = transport_for([page(sessions=["999"], has_more=True, cursor="c1")])
    with TestClient(create_app(settings(), transport=transport)) as client:
        response = client.post("/v1/task-ids/query", json={"filters": FILTERS})
    assert response.status_code == 200
    assert response.json()["task_ids"] == []
    assert response.json()["has_more"] is True
    assert response.json()["next_cursor"] == "c1"


@pytest.mark.parametrize(
    "pages,code",
    [
        ([page(["111"], has_more=True)], "UPSTREAM_PAGINATION_ERROR"),
        (
            [
                page(["111"], has_more=True, cursor="a"),
                page(["222"], has_more=True, cursor="b"),
                page(["333"], has_more=True, cursor="a"),
            ],
            "UPSTREAM_PAGINATION_ERROR",
        ),
        (
            [
                httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [{"task_id": "111"}],
                            "page_info": {"has_more": False},
                        },
                    },
                )
            ],
            "UPSTREAM_PROTOCOL_ERROR",
        ),
        (
            [httpx.Response(200, json={"code": 0, "data": {"items": []}})],
            "UPSTREAM_PROTOCOL_ERROR",
        ),
    ],
)
def test_invalid_upstream_contract_fails_without_partial_success(pages, code):
    with TestClient(create_app(settings(), transport=transport_for(pages))) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": FILTERS})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == code
    assert "task_ids" not in response.json()


@pytest.mark.parametrize(
    "limits,pages",
    [
        ({"max_pages": 1}, [page(["111"], has_more=True, cursor="c1")]),
        ({"max_task_ids": 1}, [page(["111", "222"])]),
    ],
)
def test_collection_limits_do_not_report_complete(limits, pages):
    with TestClient(
        create_app(settings(**limits), transport=transport_for(pages))
    ) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": FILTERS})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "QUERY_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"filters": {"filter_fields": []}},
        {
            "filters": {
                "filter_fields": [
                    {
                        "field_name": "repo_path",
                        "query_type": "in",
                        "values": ["repo"],
                    }
                ]
            }
        },
        {
            "filters": {
                "filter_fields": [
                    {
                        "field_name": "source_platform",
                        "query_type": "in",
                        "values": ["x"],
                    }
                ]
            }
        },
        {
            "filters": {
                "query_and_or": "or",
                "filter_fields": [
                    FILTERS["filter_fields"][0],
                    FILTERS["filter_fields"][0],
                ],
            }
        },
        {
            "filters": {
                "filter_fields": [
                    {
                        "field_name": "task_created_at",
                        "query_type": "range",
                        "values": ["2026-11-30 00:00:00", "2026-11-01 00:00:00"],
                    }
                ]
            }
        },
        {
            "filters": {
                "filter_fields": [
                    {
                        "field_name": "task_created_at",
                        "query_type": "range",
                        "values": ["2026-02-30 00:00:00", ""],
                    }
                ]
            }
        },
        {"filters": FILTERS, "limit": 201},
        {"filters": FILTERS, "limit": True},
        {"filters": FILTERS, "cursor": "illegal cursor"},
        {"filters": FILTERS, "include_trace": True},
    ],
)
def test_invalid_filters_rejected_before_upstream(body):
    def unexpected(request):
        pytest.fail("Invalid queries must not call IAM or Flux")

    with TestClient(
        create_app(settings(), transport=httpx.MockTransport(unexpected))
    ) as client:
        response = client.post("/v1/task-ids/query", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_QUERY"


def test_auth_key_and_health_checks():
    with TestClient(create_app(settings(api_key=SecretStr("my-key")))) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        response = client.post("/v1/task-ids/query", json={"filters": FILTERS})
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_valid_key_query_and_generated_api_documentation():
    with TestClient(
        create_app(
            settings(api_key=SecretStr("my-key")),
            transport=transport_for([page(["111"])]),
        )
    ) as client:
        response = client.post(
            "/v1/task-ids/query",
            json={"filters": FILTERS},
            headers={"Authorization": "Bearer my-key"},
        )
        assert response.status_code == 200
        assert response.json()["task_ids"] == ["111"]
        assert client.get("/docs").status_code == 200
        schema = client.get("/openapi.json").json()
        assert "/v1/task-ids/query" in schema["paths"]
        assert "/v1/task-ids/collect" in schema["paths"]


def test_missing_service_credential_never_uses_personal_jwt(tmp_path):
    (tmp_path / "jwt").write_text("personal-jwt")
    config = settings(service_secret=None, service_secret_file=tmp_path / "missing")
    with TestClient(create_app(config)) as client:
        assert client.get("/health/ready").status_code == 503
        response = client.post("/v1/task-ids/query", json={"filters": FILTERS})
        assert response.status_code == 503
        assert "personal-jwt" not in response.text


async def test_service_jwt_refreshes_once_and_retries_same_page():
    iam_calls = 0
    sent_tokens = []

    def handle(request):
        nonlocal iam_calls
        if request.url.path == "/auth/api/v1/jwt":
            iam_calls += 1
            return httpx.Response(200, headers={"X-Jwt-Token": f"jwt-{iam_calls}"})
        sent_tokens.append(request.headers["X-Jwt-Token"])
        if len(sent_tokens) == 1:
            return httpx.Response(200, json={"code": 2001, "data": {}})
        return page(["111"])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        api = OpenAPIClient(settings(), http)
        await api.query_tasks({"filters": FILTERS, "limit": 200})
        await api.query_tasks({"filters": FILTERS, "limit": 200})
    assert iam_calls == 2
    assert sent_tokens == ["jwt-1", "jwt-2", "jwt-2"]


async def test_rate_limit_retries_but_authz_denial_does_not(monkeypatch):
    delays = []

    async def backoff(attempt, retry_after=None):
        delays.append(attempt)

    monkeypatch.setattr(OpenAPIClient, "_backoff", staticmethod(backoff))
    responses = [httpx.Response(200, json={"code": 2003, "data": {}}), page(["111"])]
    async with httpx.AsyncClient(transport=transport_for(responses)) as http:
        api = OpenAPIClient(settings(max_retries=1), http)
        assert (await api.query_tasks({"filters": FILTERS}))["items"]
    assert delays == [0]
    denied = httpx.Response(
        200,
        json={
            "code": 2003,
            "msg": "private details",
            "data": {
                "error": {"code": "AUTHZ_DENIED", "retriable": False},
            },
        },
    )
    with TestClient(
        create_app(settings(max_retries=3), transport=transport_for([denied]))
    ) as client:
        response = client.post("/v1/task-ids/query", json={"filters": FILTERS})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_FORBIDDEN"
    assert "private details" not in response.text
    assert delays == [0]


async def test_concurrent_token_requests_share_one_iam_request():
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, headers={"X-Jwt-Token": "shared-jwt"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        auth = ServiceAccountAuth(settings(), http)
        tokens = await asyncio.gather(*(auth.get_token() for _ in range(10)))
    assert tokens == ["shared-jwt"] * 10
    assert calls == 1


def test_query_deadline_returns_error():
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, headers={"X-Jwt-Token": "jwt"})

    with TestClient(
        create_app(
            settings(query_timeout_seconds=0.01), transport=httpx.MockTransport(slow)
        )
    ) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": FILTERS})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "QUERY_TIMEOUT"


def test_expiring_token_cache_reads_jwt_exp():
    expiration = int(time.time()) + 3600
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expiration}).encode())
    token = f"header.{payload.decode().rstrip('=')}.signature"
    assert ServiceAccountAuth._expiry(token) == expiration


def test_separate_page_requests_can_traverse_beyond_collect_limits():
    requests = []
    transport = transport_for(
        [page(["111", "222"], cursor="c1", has_more=True), page(["333", "444"])],
        requests,
    )
    with TestClient(
        create_app(settings(max_pages=1, max_task_ids=1), transport=transport)
    ) as client:
        first = client.post("/v1/task-ids/query", json={"filters": FILTERS, "limit": 2})
        second = client.post(
            "/v1/task-ids/query",
            json={
                "filters": FILTERS,
                "limit": 2,
                "cursor": first.json()["next_cursor"],
            },
        )
    assert first.status_code == second.status_code == 200
    assert first.json()["has_more"] is True
    assert second.json()["has_more"] is False
    assert first.json()["task_ids"] + second.json()["task_ids"] == [
        "111",
        "222",
        "333",
        "444",
    ]
    assert requests[1]["cursor"] == "c1"
