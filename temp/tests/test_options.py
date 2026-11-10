"""Verify cascaded options, query scopes, exclusions and caching through HTTP."""

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.client import OpenAPIClient
from flux_task_service.config import Settings
from flux_task_service.models import CollectQuery, Filters, OptionsQuery
from flux_task_service.options import OptionsService
from flux_task_service.service import TaskService

BASE = {
    "filter_fields": [
        {
            "field_name": "task_created_at",
            "query_type": "range",
            "values": ["2026-11-01 00:00:00", "2026-11-03 23:59:59"],
        }
    ]
}


def config(**overrides):
    return Settings(
        _env_file=None,
        **(
            {
                "service_secret": SecretStr("test-secret"),
                "max_retries": 0,
            }
            | overrides
        ),
    )


@pytest.fixture(autouse=True)
def no_network_pacing(monkeypatch):
    async def no_pace(self):
        pass

    monkeypatch.setattr(OpenAPIClient, "_pace", no_pace)


def task(task_id, **fields):
    return {"task_id": task_id, "id_kind": "task", "task": fields}


def page(items, *, cursor=None):
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "items": items,
                "page_info": {
                    "has_more": cursor is not None,
                    "next_cursor": cursor,
                },
            },
        },
    )


def mock(pages, requests=None):
    pages = iter(pages)

    def handle(request):
        if request.url.path == "/auth/api/v1/jwt":
            return httpx.Response(200, headers={"X-Jwt-Token": "test-jwt"})
        assert request.url.path == "/openapi/v1/task"
        if requests is not None:
            requests.append(json.loads(request.content))
        return next(pages)

    return httpx.MockTransport(handle)


def options(client, *, field="category", path=None, filters=None):
    return client.post(
        "/v1/filter-options",
        json={
            "field": field,
            "filters": BASE if filters is None else filters,
            "category_path": [] if path is None else path,
        },
    )


def test_root_options_use_all_pages_and_unique_task_counts():
    transport = mock(
        [
            page(
                [
                    task("111", level_1_category="研发"),
                    task("222", level_1_category="设计"),
                    {"task_id": "999", "id_kind": "session"},
                ],
                cursor="c1",
            ),
            page(
                [
                    task("111", level_1_category="研发"),
                    task("333", level_1_category="研发"),
                ]
            ),
        ]
    )
    with TestClient(create_app(config(), transport=transport)) as client:
        response = options(client)
    assert response.status_code == 200
    result = response.json()
    assert result["category_level"] == 1
    assert result["page_count"] == 2
    assert result["scanned_count"] == 5
    assert result["matched_task_count"] == 3
    assert result["exhausted"] is True
    assert {o["value"]: o["task_count"] for o in result["options"]} == {
        "研发": 2,
        "设计": 1,
    }
    assert all(len(o["category_path"]) == 1 for o in result["options"])


@pytest.mark.parametrize(
    "path,field",
    [
        (["研发"], "level_2_category"),
        (["研发", "测试"], "level_3_category"),
    ],
)
def test_child_options_query_exact_ancestor_path(path, field):
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock([page([task("111", **{field: "自动化"})])], requests),
        )
    ) as client:
        response = options(client, path=path)
    assert response.status_code == 200
    option = response.json()["options"][0]
    assert option["category_path"] == [*path, "自动化"]
    sent = requests[0]["filters"]["filter_fields"]
    assert [(f["field_name"], f["values"]) for f in sent[1:]] == [
        (f"level_{level}_category", [value]) for level, value in enumerate(path, 1)
    ]


def test_child_options_accept_exact_ids_without_explicit_index_filter():
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111", level_2_category="测试")]),
                ],
                requests,
            ),
        )
    ) as client:
        response = options(client, path=["研发"], filters={"task_ids": ["111"]})
    assert response.status_code == 200
    assert response.json()["options"][0]["category_path"] == ["研发", "测试"]
    assert requests[0]["task_id_list"] == ["111"]
    assert requests[0]["limit"] == 50


def test_cascade_applies_and_to_single_field_or_scope():
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111", level_2_category="测试")]),
                ],
                requests,
            ),
        )
    ) as client:
        response = options(
            client, path=["研发"], filters={**BASE, "query_and_or": "or"}
        )
    assert response.status_code == 200
    assert requests[0]["filters"]["query_and_or"] == "and"


def test_cache_is_scoped_to_parent_and_filters():
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111", level_2_category="测试")]),
                    page([task("222", level_2_category="测试")]),
                    page([task("333", level_2_category="部署")]),
                    page([]),
                ],
                requests,
            ),
        )
    ) as client:
        first = options(client, path=["研发"]).json()
        cached = options(client, path=["研发"]).json()
        other = options(client, path=["设计"]).json()
        narrower = options(
            client,
            path=["研发"],
            filters={
                **BASE,
                "exclude_fields": [
                    {
                        "field_name": "task_status",
                        "query_type": "in",
                        "values": ["failed"],
                    }
                ],
            },
        )
    assert first["cached"] is False
    assert cached["cached"] is True
    assert other["cached"] is False
    assert cached["options"][0]["category_path"] == ["研发", "测试"]
    assert other["options"][0]["category_path"] == ["设计", "测试"]
    assert narrower.status_code == 200
    assert narrower.json()["cached"] is False
    assert len(requests) == 4


def test_option_scope_excludes_matching_ids_before_aggregation():
    requests = []
    filters = {
        **BASE,
        "exclude_fields": [
            {
                "field_name": "created_by",
                "query_type": "in",
                "values": ["bob"],
            }
        ],
    }
    transport = mock(
        [
            page(
                [
                    task("111", level_1_category="研发"),
                    task("222", level_1_category="设计"),
                ]
            ),
            page([task("222")]),
        ],
        requests,
    )
    with TestClient(create_app(config(), transport=transport)) as client:
        result = options(client, filters=filters).json()
    assert [item["value"] for item in result["options"]] == ["研发"]
    assert result["matched_task_count"] == 1
    assert requests[1]["task_id_list"] == ["111", "222"]
    assert requests[1]["filters"]["filter_fields"][0]["field_name"] == "created_by"
    assert "exclude_fields" not in requests[0]["filters"]


def test_owner_options_skip_redacted_and_missing_fields():
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page(
                        [
                            task("111", created_by="alice"),
                            task("222", created_by="a***"),
                            task("333"),
                        ]
                    )
                ]
            ),
        )
    ) as client:
        result = options(client, field="created_by").json()
    assert [item["value"] for item in result["options"]] == ["alice"]
    assert result["missing_value_count"] == 1
    assert result["redacted_value_count"] == 1
    assert len(result["warnings"]) == 2
    assert result["availability"] == "partial"


def test_metadata_projection_marks_hidden_fields_unavailable():
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page(
                        [
                            task(
                                "111",
                                privacy_projection="task-metadata-only.v1",
                                task_is_completed="0",
                            ),
                        ]
                    )
                ]
            ),
        )
    ) as client:
        result = options(client).json()
    assert result["options"] == []
    assert result["matched_task_count"] == 1
    assert result["availability"] == "unavailable"
    assert result["unavailable_value_count"] == 1
    assert result["missing_value_count"] == 0
    assert result["warnings"]


def test_no_matching_tasks_is_available_empty_options():
    with TestClient(create_app(config(), transport=mock([page([])]))) as client:
        result = options(client).json()
    assert result["options"] == []
    assert result["availability"] == "available"
    assert result["matched_task_count"] == 0


@pytest.mark.parametrize(
    "field", ["task_status", "task_is_completed", "worker_session_id", "task_id"]
)
def test_other_option_fields(field):
    values = {
        "task_status": "interrupted",
        "task_is_completed": "0",
        "worker_session_id": "999",
    }
    with TestClient(
        create_app(config(), transport=mock([page([task("111", **values)])]))
    ) as client:
        result = options(client, field=field).json()
    assert result["options"][0]["value"] == (
        "111" if field == "task_id" else values[field]
    )
    assert result["category_level"] is None
    assert result["options"][0]["category_path"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"field": "source_platform", "filters": BASE},
        {"field": "row_source", "filters": BASE},
        {"field": "category", "filters": BASE, "category_path": ["A", "B", "C"]},
        {"field": "task_status", "filters": BASE, "category_path": ["A"]},
        {"field": "category", "filters": BASE, "category_path": [" "]},
        {"field": "category", "filters": {**BASE, "category_path": ["A"]}},
    ],
)
def test_invalid_option_requests_are_rejected_before_network(body):
    def unexpected(request):
        pytest.fail("Invalid option queries should not contact the upstream")

    with TestClient(
        create_app(config(), transport=httpx.MockTransport(unexpected))
    ) as client:
        response = client.post("/v1/filter-options", json=body)
    assert response.status_code == 422


def test_failed_options_request_is_not_cached():
    requests = []
    with TestClient(
        create_app(
            config(max_pages=1),
            transport=mock(
                [
                    httpx.Response(500, json={"code": 9000, "data": {}}),
                    page([task("111", level_1_category="A")]),
                ],
                requests,
            ),
        )
    ) as client:
        first = options(client)
        second = options(client)
    assert first.status_code == 503
    assert "options" not in first.json()
    assert second.status_code == 200
    assert second.json()["cached"] is False
    assert len(requests) == 2


def test_options_load_in_batches_and_follow_cursor():
    requests = []
    with TestClient(
        create_app(
            config(options_pages_per_batch=1),
            transport=mock(
                [
                    page([task("111", task_is_completed="0")], cursor="c1"),
                    page([task("222", task_is_completed="1")]),
                ],
                requests,
            ),
        )
    ) as client:
        first = options(client, field="task_is_completed")
        second = client.post(
            "/v1/filter-options",
            json={
                "field": "task_is_completed",
                "filters": BASE,
                "cursor": first.json()["next_cursor"],
            },
        )
    assert first.status_code == 200
    assert first.json()["exhausted"] is False
    assert first.json()["stop_reason"] == "batch_limit"
    assert first.json()["options"][0]["value"] == "0"
    assert second.status_code == 200
    assert second.json()["options"][0]["value"] == "1"
    assert second.json()["exhausted"] is True
    assert second.json()["next_cursor"] is None
    assert requests[1]["cursor"] == "c1"


def test_hidden_classification_returns_immediately_instead_of_scanning_every_task():
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page(
                        [task("111", privacy_projection="task-metadata-only.v1")],
                        cursor="c1",
                    ),
                ],
                requests,
            ),
        )
    ) as client:
        response = options(client)
    assert response.status_code == 200
    assert response.json()["availability"] == "unavailable"
    assert response.json()["exhausted"] is False
    assert response.json()["stop_reason"] == "field_unavailable"
    assert response.json()["next_cursor"] == "c1"
    assert len(requests) == 1


def test_task_id_options_do_not_require_task_id_in_field_projection():
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page(
                        [task("111", privacy_projection="task-metadata-only.v1")],
                        cursor="c1",
                    ),
                    page([task("222", privacy_projection="task-metadata-only.v1")]),
                ]
            ),
        )
    ) as client:
        response = options(client, field="task_id")
    assert response.status_code == 200
    assert response.json()["exhausted"] is True
    assert {option["value"] for option in response.json()["options"]} == {"111", "222"}


def test_options_have_short_deadline():
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, headers={"X-Jwt-Token": "jwt"})

    with TestClient(
        create_app(
            config(options_query_timeout_seconds=0.01),
            transport=httpx.MockTransport(slow),
        )
    ) as client:
        response = options(client)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "QUERY_TIMEOUT"


def test_expired_cache_refetches():
    requests = []
    with TestClient(
        create_app(
            config(options_cache_ttl_seconds=0.01),
            transport=mock(
                [
                    page([task("111", task_status="running")]),
                    page([task("111", task_status="interrupted")]),
                ],
                requests,
            ),
        )
    ) as client:
        first = options(client, field="task_status").json()
        time.sleep(0.02)
        second = options(client, field="task_status").json()
    assert first["options"][0]["value"] == "running"
    assert second["options"][0]["value"] == "interrupted"
    assert len(requests) == 2


async def test_concurrent_option_queries_share_scan():
    requests = []
    settings = config()
    async with httpx.AsyncClient(
        transport=mock(
            [
                page([task("111", level_1_category="A")]),
            ],
            requests,
        )
    ) as http:
        service = OptionsService(
            TaskService(OpenAPIClient(settings, http), settings), settings
        )
        query = OptionsQuery(filters=BASE)
        results = await asyncio.gather(*(service.get_options(query) for _ in range(5)))
    assert len(requests) == 1
    assert sum(result.cached for result in results) == 4


def test_exclusion_union_and_category_path_keep_ancestors():
    requests = []
    filters = {
        **BASE,
        "exclude_fields": [
            {
                "field_name": "task_status",
                "query_type": "in",
                "values": ["failed"],
            }
        ],
        "exclude_category_paths": [["研发", "测试"]],
    }
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111"), task("222"), task("333")]),
                    page([task("111")]),
                    page([task("222")]),
                ],
                requests,
            ),
        )
    ) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": filters})
    assert response.status_code == 200
    assert response.json()["task_ids"] == ["333"]
    assert response.json()["excluded_count"] == 2
    assert requests[2]["task_id_list"] == ["222", "333"]
    fields = {
        f["field_name"]: f["values"] for f in requests[2]["filters"]["filter_fields"]
    }
    assert fields["level_1_category"] == ["研发"]
    assert fields["level_2_category"] == ["测试"]
    assert "task_created_at" in fields


def test_out_of_scope_exclusion_result_fails():
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111")]),
                    page([task("999")]),
                ]
            ),
        )
    ) as client:
        response = client.post(
            "/v1/task-ids/collect",
            json={
                "filters": {
                    **BASE,
                    "exclude_fields": [
                        {
                            "field_name": "task_status",
                            "query_type": "in",
                            "values": ["failed"],
                        }
                    ],
                }
            },
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "UPSTREAM_FILTER_MISMATCH"


def test_explicit_task_id_exclusion_needs_no_additional_upstream_query():
    requests = []
    with TestClient(
        create_app(
            config(),
            transport=mock(
                [
                    page([task("111"), task("222")]),
                ],
                requests,
            ),
        )
    ) as client:
        response = client.post(
            "/v1/task-ids/collect",
            json={
                "filters": {
                    **BASE,
                    "exclude_task_ids": ["111", "999"],
                }
            },
        )
    assert response.status_code == 200
    assert response.json()["task_ids"] == ["222"]
    assert response.json()["excluded_count"] == 1
    assert len(requests) == 1


def test_work_modes_filter_across_empty_matching_pages_without_upstream_predicate():
    requests = []
    filters = {
        **BASE,
        "filter_fields": [
            *BASE["filter_fields"],
            {
                "field_name": "work_mode",
                "query_type": "in",
                "values": ["coding", "general"],
            },
        ],
    }
    transport = mock(
        [
            page([task("111", work_mode="requirement")], cursor="next"),
            page([task("222", work_mode="coding"), task("333", work_mode="general")]),
        ],
        requests,
    )
    with TestClient(create_app(config(), transport=transport)) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": filters})
    assert response.status_code == 200
    assert response.json()["task_ids"] == ["222", "333"]
    assert response.json()["scanned_count"] == 3
    assert response.json()["page_count"] == 2
    assert requests[1]["cursor"] == "next"
    assert all(
        field["field_name"] != "work_mode"
        for request in requests
        for field in request["filters"]["filter_fields"]
    )


def test_exclude_multiple_work_modes_uses_candidate_scope():
    requests = []
    rows = [
        task("111", work_mode="coding"),
        task("222", work_mode="general"),
        task("333", work_mode="requirement"),
    ]
    filters = {
        **BASE,
        "exclude_fields": [
            {
                "field_name": "work_mode",
                "query_type": "in",
                "values": ["coding", "general"],
            }
        ],
    }
    with TestClient(
        create_app(config(), transport=mock([page(rows), page(rows)], requests))
    ) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": filters})
    assert response.status_code == 200
    assert response.json()["task_ids"] == ["333"]
    assert response.json()["excluded_count"] == 2
    assert requests[1]["task_id_list"] == ["111", "222", "333"]
    assert all(
        f["field_name"] != "work_mode" for f in requests[1]["filters"]["filter_fields"]
    )


def test_missing_work_mode_fails_instead_of_reporting_no_matches():
    filters = {
        **BASE,
        "filter_fields": [
            *BASE["filter_fields"],
            {"field_name": "work_mode", "query_type": "in", "values": ["coding"]},
        ],
    }
    with TestClient(
        create_app(config(), transport=mock([page([task("111")])]))
    ) as client:
        response = client.post("/v1/task-ids/collect", json={"filters": filters})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "FILTER_FIELD_UNAVAILABLE"


def test_category_multiselect_is_forwarded_as_in_values_per_level():
    requests = []
    fields = [
        *BASE["filter_fields"],
        {
            "field_name": "level_1_category",
            "query_type": "in",
            "values": ["代码工程", "测试"],
        },
        {
            "field_name": "level_2_category",
            "query_type": "in",
            "values": ["代码开发", "单元测试"],
        },
        {
            "field_name": "level_3_category",
            "query_type": "in",
            "values": ["需求开发", "单测代码生成"],
        },
    ]
    with TestClient(
        create_app(config(), transport=mock([page([task("111")])], requests))
    ) as client:
        response = client.post(
            "/v1/task-ids/collect", json={"filters": {"filter_fields": fields}}
        )
    assert response.status_code == 200
    assert requests[0]["filters"]["query_and_or"] == "and"
    for actual, expected in zip(
        requests[0]["filters"]["filter_fields"], fields, strict=True
    ):
        assert actual["field_name"] == expected["field_name"]
        assert actual["values"] == expected["values"]


def test_limit_is_canonical_and_page_size_remains_an_alias():
    filters = Filters(task_ids=["111"])
    assert CollectQuery(filters=filters, limit=50).limit == 50
    assert CollectQuery(filters=filters, page_size=50).limit == 50
    assert OptionsQuery(filters=filters, limit=50).limit == 50
    assert OptionsQuery(filters=filters, page_size=50).limit == 50
