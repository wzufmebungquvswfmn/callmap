"""Check the HTML page is connected, assets are served and secrets stay server-side."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

from flux_task_service.app import create_app
from flux_task_service.config import Settings


def test_home_and_assets_are_served_without_exposing_credentials():
    settings = Settings(
        _env_file=None,
        service_secret=SecretStr("server-secret-must-stay-private"),
        api_key=SecretStr("caller-key-must-stay-private"),
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "<title>Task reflux</title>" in page.text
        assert '"api_key_required": true' in page.text
        assert "<!--UI_CONFIG-->" not in page.text
        assert "server-secret-must-stay-private" not in page.text
        assert "caller-key-must-stay-private" not in page.text
        assert client.get("/assets/ui.css").status_code == 200
        assert client.get("/assets/ui.js").status_code == 200
        assert client.get("/assets/config.py").status_code == 404
        assert client.get("/secret").status_code == 404
        paths = client.get("/openapi.json").json()["paths"]
        assert len(paths) == 37
        assert "get" in paths["/v1/exports"]
        assert "/v1/tasks/summaries" in paths
        assert "get" in paths["/v1/chatml/{job_id}/{task_id}"]
        assert "get" in paths["/v1/tagging/tag-schema"]
        assert {"get", "post"} <= paths[
            "/v1/tagging/{tagging_id}/task/{task_id}"
        ].keys()
        assert "/v1/task-data/fornax/auth" in paths
        assert "/v1/exports/{job_id}/retry" in paths
        assert "/v1/exports/{job_id}/tasks/{task_id}/retry" in paths
        assert "/v1/exports/{job_id}/tagging" in paths
        assert "/v1/tagging/{tagging_id}/results" in paths
        assert client.get("/assets/tagging.js").status_code == 200
        assert "/v1/task-data/trace" in paths
        assert "/v1/task-data/artifacts/raw" in paths
        assert "/v1/exports" in paths
        assert "/" not in paths


def test_home_does_not_require_a_caller_key_when_not_configured():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        assert '"api_key_required": false' in client.get("/").text


def test_openapi_declares_json_response_schemas():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        spec = client.get("/openapi.json").json()

    expected = {
        ("/v1/task-data/trace", "post", "200"): "TraceResponse",
        ("/v1/task-data/metrics", "post", "200"): "MetricsResponse",
        ("/v1/exports", "post", "202"): "ExportJobResponse",
        ("/v1/exports/{job_id}", "get", "200"): "ExportJobResponse",
        ("/v1/exports/{job_id}/tagging", "post", "202"): "TaggingJobResponse",
        ("/v1/tagging/{tagging_id}", "get", "200"): "TaggingJobResponse",
        ("/v1/tagging/{tagging_id}/results", "get", "200"): "TaggingResultsPage",
        ("/v1/tagging/{tagging_id}/task/{task_id}", "get", "200"): "DynamicTaskTags",
        ("/v1/tagging/{tagging_id}/task/{task_id}", "post", "200"): "DynamicTaskTags",
    }
    for (path, method, status), name in expected.items():
        schema = spec["paths"][path][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{name}"}
        assert spec["components"]["schemas"][name]["properties"]

    metrics_query = spec["components"]["schemas"]["MetricsQuery"]
    assert set(metrics_query["properties"]) == {
        "task_id",
        "source",
        "start_ms",
        "end_ms",
    }

    for name in ("OptionsQuery", "CollectQuery", "PageQuery"):
        properties = spec["components"]["schemas"][name]["properties"]
        assert "limit" in properties
        assert "page_size" not in properties
