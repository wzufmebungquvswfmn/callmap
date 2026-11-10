"""Consumer checks for portable manifests, legacy shapes and corrupt references."""

import json
import re
import shutil
from pathlib import Path

import pytest

from flux_task_service.export_contract import (
    ContractError,
    FileReference,
    MetricValues,
    TaskDataReader,
    build_manifest,
    contract_schemas,
    main,
)


def write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def make_manifest(root, components, results, *, raw=False, finalized=True):
    manifest = build_manifest(
        root,
        root.name,
        components,
        results,
        finalized=finalized,
        task_status="ok",
        raw_requested=raw,
    )
    write(root, "data-manifest.json", manifest.model_dump())
    return TaskDataReader(root)


def component(name):
    return {
        "status": "ok",
        "source": "flux_insight_openapi",
        "file": f"tasks/111/{name}.json",
    }


def metric_values(**values):
    return {name: None for name in MetricValues.model_fields} | values


@pytest.mark.parametrize("location", ["inline", "external", "recovered", "fornax"])
def test_execution_and_raw_locations(tmp_path, location):
    root = tmp_path / "111"
    span = {"span_id": "s1"}
    execution = {
        "schema_version": "trace-read-model.v2",
        "sessions": [],
        "raw_spans": [span],
    }
    trace = {
        "source": "flux_insight_openapi",
        "format": "trace-read-model.v2",
        "execution_complete": True,
        "raw_spans_complete": True,
        "raw_spans_status": "complete",
    }
    if location == "inline":
        trace.update(data=execution, spans=[span])
    elif location == "fornax":
        trace.update(
            source="fornax",
            format="flux_insight_spans",
            spans=[span],
            window={"start_ms": 10, "end_ms": 20},
        )
        write(root, "trace/fornax.json", {"spans": [{"original": True}]})
    else:
        trace.update(data_file="tasks/111/trace/openapi.json")
        write(root, "trace/openapi.json", execution)
        if location == "external":
            trace.update(spans_file=trace["data_file"], spans_pointer="/raw_spans")
        else:
            trace.update(spans_file="tasks/111/trace/raw-spans.json")
            write(root, "trace/raw-spans.json", [span])
    write(root, "trace.json", trace)
    reader = make_manifest(root, ["trace"], {"trace": component("trace")}, raw=True)
    assert reader.validate()["valid"]
    data = reader.read_dataset("execution")
    assert data["status"] == "available"
    assert data["complete"] is True
    raw = reader.read_dataset("raw_spans")
    assert raw["documents"][0]["content"] == (
        [{"original": True}] if location == "fornax" else [span]
    )
    assert reader.read_dataset("metrics")["status"] == "not_requested"


def test_conversation_only_is_not_execution_and_directory_is_portable(tmp_path):
    root = tmp_path / "job" / "tasks" / "111"
    write(
        root,
        "trace.json",
        {"source": "flux_insight_openapi", "format": "conversation_only", "spans": []},
    )
    write(root, "conversation/0-chunk-0.json", {"messages": [{"content": "hello"}]})
    write(root, "conversation/1-messages-0.json", {"messages": []})
    write(
        root,
        "conversation.json",
        {
            "schema_version": "1.0",
            "source": "flux_insight_openapi",
            "complete": True,
            "content_available": True,
            "errors": [],
            "files": [
                "tasks/111/conversation/0-chunk-0.json",
                "tasks/111/conversation/1-messages-0.json",
            ],
            "scope": {"session": "123"},
            "omitted": ["reasoning"],
        },
    )
    make_manifest(
        root,
        ["trace"],
        {"trace": component("trace"), "conversation": component("conversation")},
    )
    copied = tmp_path / "portable" / "111"
    shutil.copytree(root, copied)
    reader = TaskDataReader(copied)
    assert reader.validate()["valid"]
    assert reader.read_dataset("execution")["status"] == "unavailable"
    assert reader.read_dataset("raw_spans")["status"] == "not_requested"
    conversation = reader.read_dataset("conversation")
    assert conversation["scope"]["omitted"] == ["reasoning"]
    assert len(conversation["documents"]) == 3


@pytest.mark.parametrize("value", ["12", True, float("nan")])
def test_invalid_metrics_do_not_break_other_datasets(tmp_path, value):
    root = tmp_path / "111"
    write(
        root,
        "metrics.json",
        {
            "source": "fornax",
            "complete": True,
            "warnings": [],
            "values": metric_values(total_tokens=value),
        },
    )
    write(root, "task.json", {"items": []})
    reader = make_manifest(
        root,
        ["task", "metrics"],
        {"task": component("task"), "metrics": component("metrics")},
    )
    assert not reader.validate()["valid"]
    assert reader.read_dataset("metrics")["status"] == "unavailable"
    assert reader.read_dataset("task")["documents"][0]["content"] == {"items": []}


def test_unknown_metrics_remain_null_and_known_zero_remains_zero(tmp_path):
    root = tmp_path / "111"
    write(
        root,
        "metrics.json",
        {
            "schema_version": "1.0",
            "source": "fornax",
            "complete": None,
            "warnings": [],
            "values": metric_values(input_tokens=None, output_tokens=0),
        },
    )
    reader = make_manifest(root, ["metrics"], {"metrics": component("metrics")})
    data = reader.read_dataset("metrics")
    assert data["complete"] is None
    values = data["documents"][0]["content"]["values"]
    assert values["input_tokens"] is None
    assert values["output_tokens"] == 0


def test_partial_download_keeps_empty_binary_and_errors(tmp_path):
    root = tmp_path / "111"
    write(root, "artifacts/manifest.json", {"files": []})
    target = root / "artifacts/files/empty.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    downloads = [
        {
            "path": "empty.bin",
            "file": "tasks/111/artifacts/files/empty.bin",
            "status": "ok",
        },
        {
            "path": "missing.bin",
            "status": "error",
            "error": {"code": "ARTIFACT_DOWNLOAD_FAILED", "message": "missing"},
        },
    ]
    write(root, "artifacts/downloads.json", downloads)
    result = component("artifacts/downloads")
    result["status"] = "partial"
    reader = make_manifest(
        root,
        ["artifacts"],
        {"artifacts": result, "artifacts/manifest": component("artifacts/manifest")},
    )
    data = reader.read_dataset("artifacts")
    assert data["status"] == "partial"
    assert data["complete"] is False
    ref = next(
        ref
        for ref in reader.manifest.datasets["artifacts"].files
        if ref.role == "artifact"
    )
    assert reader.read_file(ref) == b""
    target.unlink()
    assert not reader.validate()["valid"]
    with pytest.raises(ContractError, match="Cannot read") as exc:
        reader.read_file(ref)
    assert exc.value.code == "FILE_UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("version", "UNSUPPORTED_VERSION"),
        ("type", "INVALID_MANIFEST"),
        ("missing_dataset", "INVALID_MANIFEST"),
        ("identity", "TASK_ID_MISMATCH"),
        ("path", "INVALID_MANIFEST"),
    ],
)
def test_invalid_manifest_rejected(tmp_path, mutation, code):
    root = tmp_path / "111"
    write(root, "task.json", {})
    make_manifest(root, ["task"], {"task": component("task")})
    manifest = json.loads((root / "data-manifest.json").read_text())
    if mutation == "version":
        manifest["schema_version"] = "2.0"
    elif mutation == "type":
        manifest["finalized"] = "true"
    elif mutation == "missing_dataset":
        del manifest["datasets"]["raw_spans"]
    elif mutation == "identity":
        manifest["task_id"] = "222"
    else:
        manifest["datasets"]["task"]["files"][0]["path"] = "../secret"
    write(root, "data-manifest.json", manifest)
    with pytest.raises(ContractError) as exc:
        TaskDataReader(root)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("format", "UNSUPPORTED_FORMAT"),
        ("pointer", "INVALID_POINTER"),
        ("missing", "FILE_UNAVAILABLE"),
        ("symlink", "INVALID_PATH"),
    ],
)
def test_corrupt_reference_rejected_without_silently_returning_empty(
    tmp_path, mutation, code
):
    root = tmp_path / "111"
    write(root, "task.json", {"items": []})
    make_manifest(root, ["task"], {"task": component("task")})
    manifest = json.loads((root / "data-manifest.json").read_text())
    ref = manifest["datasets"]["task"]["files"][0]
    if mutation == "format":
        ref["format"] = "new-upstream.v99"
    elif mutation == "pointer":
        ref["pointer"] = "/items/0"
    else:
        (root / "task.json").unlink()
        if mutation == "symlink":
            write(tmp_path, "outside.json", {"secret": "not returned"})
            (root / "task.json").symlink_to(tmp_path / "outside.json")
    write(root, "data-manifest.json", manifest)
    reader = TaskDataReader(root)
    assert not reader.validate()["valid"]
    with pytest.raises(ContractError) as exc:
        reader.read_dataset("task")
    assert exc.value.code == code


def test_legacy_adapter_is_opt_in_read_only_and_requires_job_index(tmp_path):
    root = tmp_path / "tasks/111"
    write(
        root,
        "trace.json",
        {
            "source": "flux_insight_openapi",
            "format": "trace-read-model.v2",
            "data": {"schema_version": "trace-read-model.v2", "sessions": []},
            "spans": [],
        },
    )
    write(
        tmp_path,
        "job.json",
        {
            "status": "completed",
            "components": ["trace"],
            "tasks": {
                "111": {"status": "ok", "components": {"trace": component("trace")}}
            },
        },
    )
    with pytest.raises(ContractError) as exc:
        TaskDataReader(root)
    assert exc.value.code == "MANIFEST_MISSING"
    reader = TaskDataReader(root, allow_legacy=True)
    assert reader.manifest.legacy
    assert reader.validate()["valid"]
    assert reader.read_dataset("execution")["complete"] is None
    assert not (root / "data-manifest.json").exists()


def test_unfinished_manifest_and_cli_exit_status(tmp_path, monkeypatch, capsys):
    root = tmp_path / "111"
    reader = make_manifest(root, ["task"], {}, finalized=False)
    with pytest.raises(ContractError) as exc:
        reader.read_dataset("task")
    assert exc.value.code == "MANIFEST_NOT_FINALIZED"
    monkeypatch.setattr("sys.argv", ["reader", str(root)])
    assert main() == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False
    make_manifest(root, ["task"], {})
    assert main() == 0  # Unavailable data with a declared reason is a valid contract.
    assert json.loads(capsys.readouterr().out)["datasets"]["task"] == "unavailable"


def test_additive_minor_version_and_json_pointer_escaping(tmp_path):
    root = tmp_path / "111"
    write(root, "task.json", {"a/b": {"~key": {"items": []}}})
    reader = make_manifest(root, ["task"], {"task": component("task")})
    manifest = reader.manifest.model_dump()
    manifest.update(schema_version="1.9", future_field=True)
    manifest["datasets"]["task"]["files"][0]["pointer"] = "/a~1b/~0key"
    write(root, "data-manifest.json", manifest)
    reader = TaskDataReader(root)
    assert reader.read_dataset("task")["documents"][0]["content"] == {"items": []}
    with pytest.raises(ContractError) as exc:
        reader.read_file(FileReference(path="other.json", format="json-object.v1"))
    assert exc.value.code == "UNKNOWN_REFERENCE"


def test_published_schemas_match_runtime_models():
    directory = Path(__file__).resolve().parents[1] / "docs/schemas"
    for name, schema in contract_schemas().items():
        assert json.loads((directory / name).read_text()) == schema


def test_missing_metric_field_and_duplicate_json_keys_are_rejected(tmp_path):
    root = tmp_path / "111"
    values = metric_values()
    del values["total_tokens"]
    write(
        root,
        "metrics.json",
        {"source": "fornax", "complete": None, "warnings": [], "values": values},
    )
    reader = make_manifest(root, ["metrics"], {"metrics": component("metrics")})
    assert reader.validate()["errors"][0]["code"] == "INVALID_PAYLOAD"
    (root / "data-manifest.json").write_text(
        '{"schema_version":"1.0","schema_version":"2.0"}'
    )
    with pytest.raises(ContractError) as exc:
        TaskDataReader(root)
    assert exc.value.code == "INVALID_JSON"


def test_missing_conversation_page_invalidates_stored_completeness(tmp_path):
    root = tmp_path / "111"
    write(root, "conversation/present.json", {})
    write(
        root,
        "conversation.json",
        {
            "source": "flux_insight_openapi",
            "complete": True,
            "content_available": True,
            "errors": [],
            "files": [
                "tasks/111/conversation/present.json",
                "tasks/111/conversation/missing.json",
            ],
        },
    )
    reader = make_manifest(
        root, ["conversation"], {"conversation": component("conversation")}
    )
    data = reader.read_dataset("conversation")
    assert data["status"] == "partial"
    assert data["complete"] is False
    assert not reader.validate()["valid"]


def test_invalid_execution_metadata_is_isolated(tmp_path):
    root = tmp_path / "111"
    write(
        root,
        "trace.json",
        {
            "source": "flux_insight_openapi",
            "format": "trace-read-model.v2",
            "execution_complete": "true",
            "data": {"schema_version": "trace-read-model.v2", "sessions": []},
        },
    )
    write(root, "task.json", {})
    reader = make_manifest(
        root,
        ["task", "trace"],
        {"task": component("task"), "trace": component("trace")},
    )
    assert reader.read_dataset("execution")["errors"][0]["code"] == "INVALID_PAYLOAD"
    assert reader.read_dataset("task")["status"] == "available"


def documented_function(name):
    document = (
        Path(__file__).resolve().parents[1] / "docs/downstream-data-contract.md"
    ).read_text()
    block = next(
        block
        for block in re.findall(r"```python\n(.*?)\n```", document, re.DOTALL)
        if f"def {name}(" in block
    )
    namespace = {}
    exec(compile(block, "downstream-data-contract.md", "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("kind", ["full-trace", "messages-only", "chunk", "messages"])
def test_documented_conversation_reader_uses_kind_not_filename(tmp_path, kind):
    root = tmp_path / "111"
    body = {"messages": [{"content": "正文"}]}
    if kind == "full-trace":
        raw = {"trace_detail": {"data": body}}
    elif kind == "messages-only":
        raw = {"trace_detail": body}
    elif kind == "chunk":
        raw = {"data": body}
    else:
        raw = body
    name = kind if kind in {"full-trace", "messages-only"} else f"0-{kind}-0"
    write(root, f"conversation/{name}.json", raw)
    write(root, "conversation/inventory.json", {"tickets": ["not a message"]})
    files = [
        "tasks/111/conversation/inventory.json",
        f"tasks/111/conversation/{name}.json",
    ]
    if kind in {"chunk", "messages"}:
        write(
            root, "conversation/full-trace.json", {"trace_detail_status": "unavailable"}
        )
        files.insert(0, "tasks/111/conversation/full-trace.json")
    write(
        root,
        "conversation.json",
        {
            "source": "flux_insight_openapi",
            "complete": True,
            "content_available": True,
            "files": files,
            "errors": [],
        },
    )
    reader = make_manifest(
        root, ["conversation"], {"conversation": component("conversation")}
    )
    manifest = reader.manifest.model_dump()
    for index, ref in enumerate(manifest["datasets"]["conversation"]["files"]):
        renamed = f"opaque-{index}.json"
        (root / ref["path"]).rename(root / renamed)
        ref["path"] = renamed
    write(root, "data-manifest.json", manifest)
    read_bodies = documented_function("read_conversation_bodies")
    result = read_bodies(root)
    assert len(result["bodies"]) == 1
    assert result["bodies"][0]["content"] == body
    assert result["bodies"][0]["reference"]["content_kind"] == kind
    assert result["bodies"][0]["reference"]["role"] == "data"
    for ref in manifest["datasets"]["conversation"]["files"]:
        ref.pop("content_kind", None)
    write(root, "data-manifest.json", manifest)
    with pytest.raises(ValueError, match="content_kind"):
        read_bodies(root)


def test_documented_metrics_reader_isolates_unrelated_corruption(tmp_path):
    root = tmp_path / "111"
    write(root, "task.json", {})
    write(
        root,
        "metrics.json",
        {
            "source": "fornax",
            "complete": False,
            "warnings": [],
            "values": metric_values(total_tokens=12),
            "tokens_complete": True,
            "timing_complete": False,
        },
    )
    reader = make_manifest(
        root,
        ["task", "metrics"],
        {"task": component("task"), "metrics": component("metrics")},
    )
    (root / "task.json").write_text("broken")
    assert reader.validate()["valid"] is False
    read_metrics = documented_function("read_metrics_for_analysis")
    with pytest.raises(ValueError):
        read_metrics(root)
    with pytest.raises(ValueError, match="Timing"):
        read_metrics(root, accept_partial=True)
    assert (
        read_metrics(root, accept_partial=True, require_timing=False)["values"][
            "total_tokens"
        ]
        == 12
    )
