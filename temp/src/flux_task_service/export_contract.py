"""Versioned task manifests and explicit readers for exported task directories."""

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

VERSION = "1.0"
CONTRACT_ERROR_CODES = {
    "FILE_UNAVAILABLE",
    "INVALID_JSON",
    "INVALID_POINTER",
    "INVALID_PATH",
    "INVALID_PAYLOAD",
    "UNSUPPORTED_FORMAT",
    "UNSUPPORTED_VERSION",
}
DATASETS = (
    "task",
    "execution",
    "raw_spans",
    "conversation",
    "metrics",
    "artifacts",
    "versions",
    "gui",
)


class ContractError(ValueError):
    """A machine-readable error; messages are only for human diagnostics."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", allow_inf_nan=False)


class DataError(ContractModel):
    code: str
    message: str


class FileReference(ContractModel):
    path: str
    format: str
    pointer: str = ""
    role: str = "data"
    content_kind: str | None = None

    @model_validator(mode="after")
    def valid_location(self):
        path = PurePosixPath(self.path)
        if (
            not self.path
            or path.is_absolute()
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or "\x00" in self.path
        ):
            raise ValueError("File path must be a normalized task-relative path")
        if self.pointer and (
            not self.pointer.startswith("/") or re.search(r"~(?![01])", self.pointer)
        ):
            raise ValueError("Invalid JSON Pointer")
        return self


class Dataset(ContractModel):
    status: Literal["not_requested", "available", "partial", "unavailable"]
    complete: bool | None
    source: str | None
    scope: dict[str, Any]
    files: list[FileReference]
    errors: list[DataError]
    warnings: list[str]

    @model_validator(mode="after")
    def consistent_status(self):
        if self.status == "not_requested" and (self.files or self.complete is not None):
            raise ValueError("Unrequested data cannot have files or completeness")
        if self.status in {"available", "partial"} and not self.files:
            raise ValueError("Available data must reference at least one file")
        if self.complete is True and self.status in {"not_requested", "unavailable"}:
            raise ValueError("Unavailable data cannot be complete")
        if self.status == "available" and (self.complete is False or self.errors):
            raise ValueError("Incomplete data or data with errors must be partial")
        return self


class TaskManifest(ContractModel):
    schema_version: str = Field(pattern=r"^1\.[0-9]+$")
    task_id: str = Field(pattern=r"^[1-9][0-9]*$")
    finalized: bool
    task_status: str
    legacy: bool = False
    errors: list[DataError] = Field(default_factory=list)
    datasets: dict[str, Dataset]

    @model_validator(mode="after")
    def supported_contract(self):
        if not re.fullmatch(r"1\.[0-9]+", self.schema_version):
            raise ValueError("Unsupported manifest version")
        if not set(DATASETS).issubset(self.datasets):
            raise ValueError("Missing required dataset entries")
        return self


class MetricValues(ContractModel):
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    clean_total_token: int | None
    end_to_end_latency_ms: int | float | None
    total_latency_ms: int | float | None
    interaction_latency_ms: int | float | None
    latency_ms_first_resp: int | float | None
    model_call_count: int | None
    non_cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None


class MetricsPayload(ContractModel):
    schema_version: str = Field(default=VERSION, pattern=r"^1\.[0-9]+$")
    source: Literal["flux_insight_openapi", "fornax", "task_detail"]
    complete: bool | None
    values: MetricValues
    warnings: list[str]
    execution_complete: bool | None = None
    token_sources: list[dict[str, Any]] = Field(default_factory=list)
    token_field_sources: dict[str, str] = Field(default_factory=dict)
    token_scope: dict[str, Any] | None = None
    timing_source: str | None = None
    total_latency_available: bool | None = None
    total_latency_complete: bool | None = None
    first_response_complete: bool | None = None
    tokens_available: bool | None = None
    tokens_complete: bool | None = None
    timing_complete: bool | None = None
    turn_timing_complete: bool | None = None
    first_response_available: bool | None = None


class ConversationPayload(ContractModel):
    schema_version: str = Field(default=VERSION, pattern=r"^1\.[0-9]+$")
    source: str
    complete: bool | None
    content_available: bool
    files: list[str]
    errors: list[dict[str, Any]]


def contract_schemas() -> dict[str, dict]:
    """Generate the structural schemas; reference and cross-field checks run in code."""
    manifest = TaskManifest.model_json_schema()
    manifest["properties"]["datasets"].update(
        required=list(DATASETS),
        properties={name: {"$ref": "#/$defs/Dataset"} for name in DATASETS},
    )
    schemas = {
        "task-manifest.schema.json": manifest,
        "metrics.schema.json": MetricsPayload.model_json_schema(),
        "conversation-summary.schema.json": ConversationPayload.model_json_schema(),
    }
    for schema in schemas.values():
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schemas


def read_json(path: Path) -> Any:
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_bytes(),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except OSError as exc:
        raise ContractError("FILE_UNAVAILABLE", f"Cannot read {path.name}") from exc
    except ValueError as exc:
        raise ContractError("INVALID_JSON", f"Invalid JSON in {path.name}") from exc


def resolve_pointer(value: Any, pointer: str) -> Any:
    try:
        for part in pointer.split("/")[1:]:
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", key):
                    raise KeyError(key)
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise KeyError(key)
        return value
    except (KeyError, IndexError) as exc:
        raise ContractError("INVALID_POINTER", "JSON Pointer does not resolve") from exc


def read_reference(root: Path, ref: FileReference) -> Any:
    path = root / ref.path
    if not path.resolve().is_relative_to(root.resolve()):
        raise ContractError("INVALID_PATH", "Reference escapes the task directory")
    if ref.format == "bytes.v1":
        if ref.pointer:
            raise ContractError("INVALID_POINTER", "Binary files have no JSON Pointer")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ContractError("FILE_UNAVAILABLE", f"Cannot read {ref.path}") from exc
    formats = {
        "json-object.v1",
        "json-records.v1",
        "trace-read-model.v2",
        "flux.normalized-spans.v1",
        "fornax.spans.v1",
        "flux.metrics.v1",
        "flux.conversation-summary.v1",
        "flux.downloads.v1",
    }
    if ref.format not in formats:
        raise ContractError("UNSUPPORTED_FORMAT", f"Unsupported format: {ref.format}")
    value = resolve_pointer(read_json(path), ref.pointer)
    try:
        if ref.format in {
            "json-records.v1",
            "flux.normalized-spans.v1",
            "fornax.spans.v1",
            "flux.downloads.v1",
        }:
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise ValueError("Expected an array of objects")
            if ref.format == "flux.downloads.v1":
                for item in value:
                    if not isinstance(item.get("path"), str) or item.get(
                        "status"
                    ) not in {
                        "ok",
                        "error",
                    }:
                        raise ValueError("Invalid download record")
                    if item["status"] == "ok" and not isinstance(item.get("file"), str):
                        raise ValueError("Successful download has no file")
                    if item["status"] == "error" and not isinstance(
                        item.get("error"), dict
                    ):
                        raise ValueError("Failed download has no error")
        else:
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            if ref.format.startswith("flux."):
                version = value.get("schema_version", VERSION)
                if not isinstance(version, str) or not re.fullmatch(
                    r"1\.[0-9]+", version
                ):
                    raise ContractError(
                        "UNSUPPORTED_VERSION", "Unsupported payload version"
                    )
            if ref.format == "flux.metrics.v1":
                MetricsPayload.model_validate(value)
            elif ref.format == "flux.conversation-summary.v1":
                ConversationPayload.model_validate(value)
            elif ref.format == "trace-read-model.v2":
                if value.get("schema_version") != "trace-read-model.v2":
                    raise ValueError("Execution format does not match its declaration")
                if not isinstance(value.get("sessions"), list):
                    raise ValueError("Execution sessions must be an array")
    except ValueError as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("INVALID_PAYLOAD", f"Invalid {ref.format} payload") from exc
    return value


def _errors(result: dict) -> list[dict]:
    values = []
    if isinstance(result.get("error"), dict):
        values.append(result["error"])
    for key in ("errors", "fallback_errors"):
        errors = result.get(key, [])
        values.extend(errors.values() if isinstance(errors, dict) else errors)
    return [
        {
            "code": str(item.get("code", "UPSTREAM_ERROR")),
            "message": str(item.get("message", "Data acquisition failed")),
        }
        for item in values
        if isinstance(item, dict)
    ]


def build_manifest(
    root: Path,
    task_id: str,
    components: list[str],
    results: dict,
    *,
    finalized: bool,
    task_status: str,
    raw_requested: bool | None,
    legacy: bool = False,
) -> TaskManifest:
    """Describe existing outputs without changing their original contents."""
    datasets = {}
    prefix = f"tasks/{task_id}/"

    def reference(path, fmt="json-object.v1", pointer="", role="data"):
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ContractError("INVALID_PATH", "Expected a reference to this task")
        try:
            return FileReference(
                path=path[len(prefix) :], format=fmt, pointer=pointer, role=role
            )
        except ValidationError as exc:
            raise ContractError("INVALID_PATH", "Invalid task file reference") from exc

    for name in DATASETS:
        component = "trace" if name in {"execution", "raw_spans"} else name
        result = results.get(component, {})
        requested = component in components or bool(result)
        if name == "conversation" and "trace" in components:
            requested = True
        if name == "raw_spans" and raw_requested is False:
            requested = False
        entry = {
            "status": "unavailable" if requested else "not_requested",
            "complete": None,
            "source": result.get("source"),
            "scope": {},
            "files": [],
            "errors": _errors(result) if requested else [],
            "warnings": list(result.get("warnings", [])) if requested else [],
        }
        if not requested:
            datasets[name] = Dataset.model_validate(entry)
            continue
        refs = []
        usable = False
        try:
            if name in {"execution", "raw_spans"} and result.get("file"):
                trace = read_reference(root, reference(result["file"]))
                if not isinstance(trace, dict):
                    raise ContractError("INVALID_PAYLOAD", "Trace must be an object")
                entry["source"] = trace.get("source")
                entry["scope"] = {
                    "completeness_scope": trace.get("completeness_scope"),
                    "window": trace.get("window"),
                    "raw_recovery": trace.get("raw_recovery"),
                }
                if name == "execution":
                    entry["complete"] = trace.get("execution_complete")
                    if trace.get("format") == "flux_insight_spans":
                        refs.append(
                            reference(
                                result["file"], "flux.normalized-spans.v1", "/spans"
                            )
                        )
                    elif trace.get("format") != "conversation_only":
                        fmt = trace.get("format", "unknown")
                        refs.append(
                            reference(
                                trace.get("data_file", result["file"]),
                                fmt,
                                "" if "data_file" in trace else "/data",
                            )
                        )
                else:
                    entry["complete"] = trace.get("raw_spans_complete")
                    raw_status = trace.get("raw_spans_status")
                    if raw_status == "not_requested":
                        entry.update(status="not_requested", complete=None, errors=[])
                    elif trace.get("source") == "fornax":
                        refs.append(
                            reference(
                                prefix + "trace/fornax.json",
                                "fornax.spans.v1",
                                "/spans",
                            )
                        )
                    elif trace.get("spans_file"):
                        refs.append(
                            reference(
                                trace["spans_file"],
                                "json-records.v1",
                                trace.get("spans_pointer", ""),
                            )
                        )
                    elif trace.get("spans") or raw_status == "complete":
                        refs.append(
                            reference(result["file"], "json-records.v1", "/spans")
                        )
            elif name == "conversation" and result.get("file"):
                summary_ref = reference(
                    result["file"], "flux.conversation-summary.v1", role="summary"
                )
                summary = read_reference(root, summary_ref)
                refs.append(summary_ref)
                entry["complete"] = summary["complete"]
                entry["source"] = summary["source"]
                entry["scope"] = {
                    key: summary.get(key)
                    for key in (
                        "mode",
                        "scope",
                        "coverage",
                        "omitted",
                        "resources_not_embedded",
                        "completeness_scope",
                    )
                }
                entry["errors"].extend(_errors(summary))
                for path in summary["files"]:
                    ref = reference(path)
                    document_name = PurePosixPath(ref.path).stem
                    if document_name in {"full-trace", "messages-only", "inventory"}:
                        ref.content_kind = document_name
                    else:
                        batch = re.fullmatch(
                            r"[0-9]+-(chunk|messages)-[0-9]+", document_name
                        )
                        if batch:
                            ref.content_kind = batch[1]
                    refs.append(ref)
                usable = summary["content_available"] and bool(summary["files"])
            elif name == "artifacts":
                manifest_result = results.get("artifacts/manifest", {})
                entry["errors"].extend(_errors(manifest_result))
                for path, role in (
                    (manifest_result.get("file"), "initial-manifest"),
                    (result.get("refreshed_manifest_file"), "refreshed-manifest"),
                ):
                    if path:
                        refs.append(reference(path, role=role))
                if result.get("file"):
                    download_ref = reference(
                        result["file"], "flux.downloads.v1", role="download-index"
                    )
                    downloads = read_reference(root, download_ref)
                    entry["errors"].extend(
                        error
                        for item in downloads
                        if item["status"] == "error"
                        for error in _errors(item)
                    )
                    refs.append(download_ref)
                    refs.extend(
                        reference(item["file"], "bytes.v1", role="artifact")
                        for item in downloads
                        if item["status"] == "ok"
                    )
                    entry["scope"] = {
                        "selection_policy": "initial-manifest-paths",
                        "content_validation": "opaque",
                    }
                    # This covers selected manifest paths, not all task history.
                    entry["complete"] = all(
                        item["status"] == "ok" for item in downloads
                    )
                    usable = True
            elif result.get("file"):
                fmt = "flux.metrics.v1" if name == "metrics" else "json-object.v1"
                refs.append(reference(result["file"], fmt))
                if name == "metrics":
                    metrics = read_reference(root, refs[0])
                    entry["complete"] = metrics["complete"]
                    entry["scope"] = {
                        key: value
                        for key, value in metrics.items()
                        if key not in {"values", "warnings", "source", "schema_version"}
                    }
                if result.get("snapshot_file"):
                    refs.append(
                        reference(result["snapshot_file"], "bytes.v1", role="snapshot")
                    )
                child_prefix = "diffs/" if name == "versions" else "gui/screenshots/"
                if name in {"versions", "gui"}:
                    for key, child in results.items():
                        if key.startswith(child_prefix):
                            entry["errors"].extend(_errors(child))
                            entry["warnings"].extend(child.get("warnings", []))
                            if child.get("file"):
                                refs.append(
                                    reference(
                                        child["file"],
                                        "bytes.v1"
                                        if name == "gui"
                                        else "json-object.v1",
                                        role="screenshot" if name == "gui" else "diff",
                                    )
                                )
            for ref in refs:
                try:
                    # Binary contents are opaque; existence is checked without loading.
                    if ref.format == "bytes.v1":
                        path = root / ref.path
                        if (
                            not path.resolve().is_relative_to(root.resolve())
                            or not path.is_file()
                        ):
                            raise ContractError(
                                "FILE_UNAVAILABLE",
                                "Attachment is missing or outside task",
                            )
                    else:
                        read_reference(root, ref)
                    entry["files"].append(ref.model_dump())
                except ContractError as exc:
                    entry["errors"].append({"code": exc.code, "message": str(exc)})
        except (ContractError, ValidationError) as exc:
            entry["errors"].append(
                {
                    "code": exc.code
                    if isinstance(exc, ContractError)
                    else "INVALID_PAYLOAD",
                    "message": str(exc),
                }
            )
        if name not in {"conversation", "artifacts"}:
            usable = bool(entry["files"])
        if usable and entry["files"]:
            entry["status"] = (
                "partial"
                if entry["errors"]
                or entry["complete"] is False
                or (
                    name not in {"execution", "raw_spans", "metrics"}
                    and result.get("status") == "partial"
                )
                else "available"
            )
        elif entry["status"] != "not_requested":
            entry["status"] = "unavailable"
            if not entry["errors"]:
                entry["errors"].append(
                    {
                        "code": "DATA_UNAVAILABLE" if finalized else "DATA_PENDING",
                        "message": "No usable data was exported",
                    }
                )
        if entry["complete"] is True and (
            entry["status"] in {"not_requested", "unavailable"}
            or any(error["code"] in CONTRACT_ERROR_CODES for error in entry["errors"])
        ):
            entry["complete"] = False
        try:
            datasets[name] = Dataset.model_validate(entry)
        except ValidationError:
            datasets[name] = Dataset(
                status="unavailable",
                complete=None,
                source=None,
                scope={},
                files=[],
                errors=[
                    DataError(
                        code="INVALID_PAYLOAD", message=f"Invalid {name} metadata"
                    )
                ],
                warnings=[],
            )
    return TaskManifest(
        schema_version=VERSION,
        task_id=task_id,
        finalized=finalized,
        task_status=task_status,
        legacy=legacy,
        datasets=datasets,
        errors=[
            error
            for key in ("export", "timeout")
            for error in _errors(results.get(key, {}))
        ],
    )


class TaskDataReader:
    """Read a task directory; legacy adaptation is explicit and never rewrites files."""

    def __init__(self, task_dir: str | Path, *, allow_legacy: bool = False):
        self.root = Path(task_dir).resolve()
        path = self.root / "data-manifest.json"
        if not path.exists():
            if not allow_legacy:
                raise ContractError("MANIFEST_MISSING", "Task data manifest is missing")
            self.manifest = self._legacy_manifest()
        else:
            data = read_json(path)
            if not isinstance(data, dict) or not isinstance(
                data.get("schema_version"), str
            ):
                raise ContractError("INVALID_MANIFEST", "Manifest version is missing")
            if not re.fullmatch(r"1\.[0-9]+", data["schema_version"]):
                raise ContractError(
                    "UNSUPPORTED_VERSION", "Unsupported manifest version"
                )
            try:
                self.manifest = TaskManifest.model_validate(data)
            except ValidationError as exc:
                raise ContractError(
                    "INVALID_MANIFEST", "Manifest schema validation failed"
                ) from exc
        if self.manifest.task_id != self.root.name:
            raise ContractError(
                "TASK_ID_MISMATCH", "Directory and manifest task ID differ"
            )

    def _legacy_manifest(self) -> TaskManifest:
        job = read_json(self.root.parent.parent / "job.json")
        task_id = self.root.name
        if self.root.parent.name != "tasks" or not isinstance(job, dict):
            raise ContractError(
                "UNSUPPORTED_LEGACY", "Legacy adapter requires original job directory"
            )
        tasks = job.get("tasks")
        components = job.get("components")
        if (
            not isinstance(tasks, dict)
            or not isinstance(components, list)
            or not all(isinstance(item, str) for item in components)
        ):
            raise ContractError("UNSUPPORTED_LEGACY", "Invalid legacy job index")
        record = tasks.get(task_id)
        if not isinstance(record, dict) or not isinstance(
            record.get("components"), dict
        ):
            raise ContractError(
                "UNSUPPORTED_LEGACY", "Legacy job has no task component index"
            )
        if not all(isinstance(value, dict) for value in record["components"].values()):
            raise ContractError("UNSUPPORTED_LEGACY", "Invalid legacy components")
        return build_manifest(
            self.root,
            task_id,
            components,
            record["components"],
            finalized=job.get("status")
            in {"completed", "partial", "failed", "cancelled"},
            task_status=record.get("status", "unknown"),
            raw_requested=None,
            legacy=True,
        )

    def read_dataset(self, name: str) -> dict:
        """Return a fixed envelope; documents retain their declared source format."""
        if not self.manifest.finalized:
            raise ContractError(
                "MANIFEST_NOT_FINALIZED", "Task export is still unfinished"
            )
        if name not in self.manifest.datasets:
            raise ContractError("UNKNOWN_DATASET", f"Unknown dataset: {name}")
        entry = self.manifest.datasets[name]
        result = entry.model_dump()
        result["documents"] = [
            {"reference": ref.model_dump(), "content": read_reference(self.root, ref)}
            for ref in entry.files
            if ref.format != "bytes.v1"
        ]
        return result

    def read_file(self, ref: FileReference) -> Any:
        """Read an explicitly selected reference, including opaque binary files."""
        if not self.manifest.finalized:
            raise ContractError(
                "MANIFEST_NOT_FINALIZED", "Task export is still unfinished"
            )
        if not any(ref in entry.files for entry in self.manifest.datasets.values()):
            raise ContractError(
                "UNKNOWN_REFERENCE", "Reference is not in this manifest"
            )
        return read_reference(self.root, ref)

    def validate(self) -> dict:
        """Validate each reference independently; acquisition errors remain metadata."""
        errors = []
        if not self.manifest.finalized:
            errors.append({"code": "MANIFEST_NOT_FINALIZED", "dataset": None})
        for name, entry in self.manifest.datasets.items():
            errors.extend(
                {"dataset": name, **error.model_dump()}
                for error in entry.errors
                if error.code in CONTRACT_ERROR_CODES
            )
            for ref in entry.files:
                try:
                    if ref.format == "bytes.v1":
                        path = self.root / ref.path
                        if (
                            not path.resolve().is_relative_to(self.root)
                            or not path.is_file()
                        ):
                            raise ContractError(
                                "FILE_UNAVAILABLE",
                                "Attachment is missing or outside task",
                            )
                    else:
                        read_reference(self.root, ref)
                except ContractError as exc:
                    errors.append(
                        {
                            "dataset": name,
                            "path": ref.path,
                            "code": exc.code,
                            "message": str(exc),
                        }
                    )
        return {
            "task_id": self.manifest.task_id,
            "schema_version": self.manifest.schema_version,
            "legacy": self.manifest.legacy,
            "valid": not errors,
            "datasets": {
                name: entry.status for name, entry in self.manifest.datasets.items()
            },
            "errors": errors,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path, nargs="?")
    parser.add_argument("--allow-legacy", action="store_true")
    parser.add_argument("--write-schemas", type=Path)
    args = parser.parse_args()
    if args.write_schemas is not None:
        args.write_schemas.mkdir(parents=True, exist_ok=True)
        for name, schema in contract_schemas().items():
            (args.write_schemas / name).write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return 0
    if args.task_dir is None:
        parser.error("task_dir is required unless --write-schemas is used")
    try:
        report = TaskDataReader(
            args.task_dir, allow_legacy=args.allow_legacy
        ).validate()
    except ContractError as exc:
        report = {"valid": False, "errors": [{"code": exc.code, "message": str(exc)}]}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
