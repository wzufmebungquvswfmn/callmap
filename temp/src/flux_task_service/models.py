"""Request validation follows the local Flux Insight Task OpenAPI contract."""

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StrictStr,
    model_validator,
)

FIELD_OPERATORS = {
    "created_by": "in",
    "worker_session_id": "in",
    "task_status": "in",
    "work_mode": "in",
    "task_is_completed": "in",
    "task_created_at": "range",
    "updated_at": "range",
    "task_updated_at": "range",
    "level_1_category": "in",
    "level_2_category": "in",
    "level_3_category": "in",
}
INDEXED_FIELDS = {
    "created_by",
    "worker_session_id",
    "task_status",
    "task_is_completed",
    "task_created_at",
    "updated_at",
}
FilterName = Literal[
    "created_by",
    "worker_session_id",
    "task_status",
    "task_is_completed",
    "task_created_at",
    "updated_at",
    "task_updated_at",
    "work_mode",
    "level_1_category",
    "level_2_category",
    "level_3_category",
]
OptionName = Literal[
    "category",
    "created_by",
    "task_status",
    "task_is_completed",
    "worker_session_id",
    "task_id",
]
CategoryValue = Annotated[StrictStr, Field(min_length=1, max_length=1024)]
CategoryPath = Annotated[list[CategoryValue], Field(min_length=1, max_length=3)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FilterField(RequestModel):
    field_name: FilterName
    field_type: Literal["string"] = "string"
    query_type: Literal["in", "range"]
    values: list[StrictStr] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_field(self) -> Self:
        operator = FIELD_OPERATORS.get(self.field_name)
        if operator is None:
            raise ValueError(f"Unsupported filter field: {self.field_name}")
        if self.query_type != operator:
            raise ValueError(f"{self.field_name} requires {operator}")
        if any(len(value) > 1024 for value in self.values):
            raise ValueError("Filter values must be at most 1024 characters")
        if operator == "in":
            if any(not value.strip() for value in self.values):
                raise ValueError("in values cannot be empty")
        else:
            if len(self.values) != 2:
                raise ValueError("range requires exactly two bounds")
            for value in self.values:
                if value:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
                        raise ValueError("Time bounds require YYYY-MM-DD HH:MM:SS")
                    datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            lower, upper = self.values
            if lower and upper and lower > upper:
                raise ValueError("range lower bound exceeds upper bound")
        return self


class Filters(RequestModel):
    query_and_or: Literal["and", "or"] = "and"
    filter_fields: list[FilterField] = Field(default_factory=list, max_length=15)
    category_path: list[CategoryValue] = Field(default_factory=list, max_length=3)
    task_ids: list[StrictStr] = Field(default_factory=list, max_length=200)
    exclude_task_ids: list[StrictStr] = Field(default_factory=list, max_length=200)
    exclude_fields: list[FilterField] = Field(default_factory=list, max_length=15)
    exclude_category_paths: list[CategoryPath] = Field(
        default_factory=list, max_length=15
    )

    @model_validator(mode="after")
    def validate_group(self) -> Self:
        fields = self.include_fields()
        if not fields and not self.task_ids:
            raise ValueError("At least one inclusion filter or task ID is required")
        if len(fields) > 15:
            raise ValueError("At most 15 inclusion filters including category levels")
        if self.query_and_or == "or" and len(fields) > 1:
            raise ValueError("Multi-field OR is not supported by Flux OpenAPI")
        if fields and not any(field.field_name in INDEXED_FIELDS for field in fields):
            raise ValueError("At least one indexed filter field is required")
        for task_id in [*self.task_ids, *self.exclude_task_ids]:
            if not re.fullmatch(r"[0-9]{1,64}", task_id) or not task_id.strip("0"):
                raise ValueError("task_ids must contain nonzero numeric strings")
        for path in [self.category_path, *self.exclude_category_paths]:
            if any(not value.strip() for value in path):
                raise ValueError("Category values cannot be blank")
        return self

    def include_fields(self) -> list[FilterField]:
        return [*self.filter_fields, *category_fields(self.category_path)]

    def upstream_body(self) -> dict:
        body: dict = {}
        # Task OpenAPI rejects work_mode (1002); evaluate it on returned tasks.
        fields = [
            field for field in self.include_fields() if field.field_name != "work_mode"
        ]
        if fields:
            body["filters"] = {
                "query_and_or": self.query_and_or,
                "filter_fields": [field.model_dump() for field in fields],
            }
        if self.task_ids:
            body["task_id_list"] = list(dict.fromkeys(self.task_ids))
        return body

    def exclusion_groups(self) -> list[list[FilterField]]:
        return [
            *[[field] for field in self.exclude_fields],
            *[category_fields(path) for path in self.exclude_category_paths],
        ]


def category_fields(path: list[str]) -> list[FilterField]:
    return [
        FilterField(
            field_name=f"level_{level}_category", query_type="in", values=[value]
        )
        for level, value in enumerate(path, 1)
    ]


class OptionsQuery(RequestModel):
    field: OptionName = "category"
    filters: Filters
    category_path: list[CategoryValue] = Field(default_factory=list, max_length=2)
    limit: int = Field(
        default=200,
        ge=1,
        le=200,
        strict=True,
        validation_alias=AliasChoices("limit", "page_size"),
    )
    cursor: StrictStr | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if self.cursor and not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", self.cursor):
            raise ValueError("cursor must be base64url")
        if self.field != "category" and self.category_path:
            raise ValueError("category_path is only valid for category options")
        if any(not value.strip() for value in self.category_path):
            raise ValueError("Category values cannot be blank")
        if self.field == "category":
            ancestors = len(self.category_path)
            if len(self.filters.category_path) > ancestors:
                raise ValueError("Do not preselect the category level being requested")
            if (
                self.filters.category_path
                != self.category_path[: len(self.filters.category_path)]
            ):
                raise ValueError("Conflicting category paths")
            for item in self.filters.filter_fields:
                if item.field_name.startswith("level_"):
                    level = int(item.field_name[6])
                    if level > ancestors:
                        raise ValueError(
                            "Do not filter the category level being requested"
                        )
                    if self.category_path[level - 1] not in item.values:
                        raise ValueError("Conflicting category ancestor filter")
        return self


class PageQuery(RequestModel):
    filters: Filters
    limit: int = Field(default=200, ge=1, le=200, strict=True)
    cursor: StrictStr | None = Field(default=None, max_length=8192)

    @model_validator(mode="after")
    def validate_cursor(self) -> Self:
        if self.cursor and not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", self.cursor):
            raise ValueError("cursor must be base64url")
        return self


class CollectQuery(RequestModel):
    filters: Filters
    limit: int = Field(
        default=200,
        ge=1,
        le=200,
        strict=True,
        validation_alias=AliasChoices("limit", "page_size"),
    )


class PageResult(BaseModel):
    task_ids: list[str]
    count: int
    scanned_count: int
    skipped_session_count: int
    has_more: bool
    next_cursor: str | None
    excluded_count: int = 0


class CollectResult(BaseModel):
    task_ids: list[str]
    count: int
    scanned_count: int
    skipped_session_count: int
    page_count: int
    exhausted: Literal[True] = True
    excluded_count: int = 0


class FilterOption(BaseModel):
    value: str
    label: str
    task_count: int
    category_path: list[str] | None = None


class OptionsResult(BaseModel):
    field: OptionName
    options: list[FilterOption]
    category_path: list[str]
    category_level: int | None
    matched_task_count: int
    scanned_count: int
    page_count: int
    missing_value_count: int
    redacted_value_count: int
    unavailable_value_count: int = 0
    availability: Literal["available", "partial", "unavailable"] = "available"
    exhausted: bool = True
    next_cursor: str | None = None
    stop_reason: Literal["batch_limit", "field_unavailable"] | None = None
    source: Literal["task_data"] = "task_data"
    cached: bool = False
    warnings: list[str] = Field(default_factory=list)


TaskID = Annotated[StrictStr, Field(pattern=r"^[0-9]{1,64}$")]


class TaskIdentity(RequestModel):
    task_id: TaskID | None = None
    worker_session_id: TaskID | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if (self.task_id is None) == (self.worker_session_id is None):
            raise ValueError("Provide exactly one task_id or worker_session_id")
        value = self.task_id or self.worker_session_id
        if int(value) <= 0 or int(value) > 2**63 - 1:
            raise ValueError("Artifact identity must be a positive int64 string")
        return self


class ArtifactQuery(TaskIdentity):
    path: StrictStr = Field(min_length=1, max_length=4096)


class VersionDiffQuery(TaskIdentity):
    from_version_id: int | None = Field(default=None, ge=0, strict=True)
    to_version_id: int | None = Field(default=None, ge=0, strict=True)
    from_conversation_version: int | None = Field(default=None, ge=0, strict=True)
    to_conversation_version: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        for lower, upper in (
            (self.from_version_id, self.to_version_id),
            (self.from_conversation_version, self.to_conversation_version),
        ):
            if (lower is None) != (upper is None):
                raise ValueError("Both interval bounds are required")
            if lower is not None and lower > upper:
                raise ValueError("Interval lower bound exceeds upper bound")
        if self.from_version_id is None and self.from_conversation_version is None:
            raise ValueError("Provide a version or conversation interval")
        return self


class TraceQuery(RequestModel):
    task_id: TaskID
    source: Literal["auto", "openapi", "fornax"] = "auto"
    conversation_mode: Literal["full", "messages"] = "full"
    include_raw_spans: bool = Field(default=False, strict=True)
    recover_raw_spans: bool = Field(default=False, strict=True)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.recover_raw_spans and not self.include_raw_spans:
            raise ValueError("recover_raw_spans requires include_raw_spans=true")
        if not int(self.task_id):
            raise ValueError("task_id must be nonzero")
        if self.start_ms is not None and self.end_ms is not None:
            if self.start_ms > self.end_ms:
                raise ValueError("start_ms exceeds end_ms")
        return self


class MetricsQuery(RequestModel):
    """Metrics only needs the task, source and optional Fornax window."""

    task_id: TaskID
    source: Literal["auto", "openapi", "fornax"] = "auto"
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if not int(self.task_id):
            raise ValueError("task_id must be nonzero")
        if self.start_ms is not None and self.end_ms is not None:
            if self.start_ms > self.end_ms:
                raise ValueError("start_ms exceeds end_ms")
        return self


class TaskDataQuery(RequestModel):
    task_id: TaskID

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if not int(self.task_id):
            raise ValueError("task_id must be nonzero")
        return self


class ConversationQuery(RequestModel):
    task_id: TaskID | None = None
    session_id: TaskID | None = None
    action: Literal["inventory", "chunk", "messages"] = "inventory"
    ticket: StrictStr | None = Field(default=None, max_length=24000)
    cursor: StrictStr | None = Field(default=None, max_length=24000)

    @model_validator(mode="after")
    def validate_conversation(self) -> Self:
        if (self.task_id is None) == (self.session_id is None):
            raise ValueError("Provide exactly one task_id or standalone session_id")
        value = self.task_id or self.session_id
        if not int(value) or len(value) > 20:
            raise ValueError(
                "Conversation identity must be nonzero and at most 20 digits"
            )
        if self.action == "inventory" and (
            self.ticket is not None or self.cursor is not None
        ):
            raise ValueError("Inventory does not accept ticket/cursor")
        if self.action != "inventory" and not self.ticket:
            raise ValueError("Chunk/messages require an inventory ticket")
        return self


ExportComponent = Literal[
    "task", "trace", "metrics", "artifacts", "versions", "gui", "conversation"
]


class ExportQuery(RequestModel):
    task_ids: list[TaskID] = Field(default_factory=list, max_length=10000)
    filters: Filters | None = None
    conversation_mode: Literal["full", "messages"] = "full"
    include_raw_spans: bool = Field(default=False, strict=True)
    recover_raw_spans: bool = Field(default=False, strict=True)
    components: list[ExportComponent] = Field(
        default_factory=lambda: [
            "task",
            "trace",
            "metrics",
            "artifacts",
            "versions",
            "gui",
            "conversation",
        ],
        min_length=1,
        max_length=7,
    )
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.recover_raw_spans and not self.include_raw_spans:
            raise ValueError("recover_raw_spans requires include_raw_spans=true")
        if bool(self.task_ids) == (self.filters is not None):
            raise ValueError("Provide task_ids or filters, exactly one")
        if any(not int(task_id) for task_id in self.task_ids):
            raise ValueError("task_ids must be nonzero")
        if self.start_ms is not None and self.end_ms is not None:
            if self.start_ms > self.end_ms:
                raise ValueError("start_ms exceeds end_ms")
        self.task_ids = list(dict.fromkeys(self.task_ids))
        self.components = list(dict.fromkeys(self.components))
        return self


class ExportRetryQuery(RequestModel):
    """Optional overrides; the source export supplies scope and other parameters."""

    components: list[ExportComponent] | None = Field(
        default=None, min_length=1, max_length=7
    )
    conversation_mode: Literal["full", "messages"] | None = None
    include_raw_spans: bool | None = Field(default=None, strict=True)
    recover_raw_spans: bool | None = Field(default=None, strict=True)
    start_ms: int | None = Field(default=None, ge=0, strict=True)
    end_ms: int | None = Field(default=None, ge=0, strict=True)


class FornaxAuthQuery(RequestModel):
    action: Literal["reload", "refresh", "update"] = "reload"
    jwt: SecretStr | None = None

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.action == "update") != (self.jwt is not None):
            raise ValueError("Only update requires jwt")
        return self


class TraceResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    source: str
    format: str | None = None
    complete: bool | None = None
    execution_complete: bool | None = None
    conversation_complete: bool | None = None
    conversation: dict[str, Any] | None = None
    completeness_scope: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    spans: list[Any] = Field(default_factory=list)
    raw_spans_requested: bool | None = None
    raw_spans_complete: bool | None = None
    raw_spans_status: str | None = None
    raw_recovery: dict[str, Any] | None = None
    tree: str | None = None
    window: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    fallback_errors: dict[str, Any] = Field(default_factory=dict)
    openapi_data: dict[str, Any] | None = None


class MetricsValues(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    non_cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    clean_total_token: int | None = None
    end_to_end_latency_ms: int | None = None
    total_latency_ms: int | None = None
    interaction_latency_ms: int | None = None
    latency_ms_first_resp: int | None = None
    model_call_count: int | None = None


class MetricsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    source: str
    complete: bool | None = None
    execution_complete: bool | None = None
    values: MetricsValues
    warnings: list[str] = Field(default_factory=list)
    fallback_errors: dict[str, Any] = Field(default_factory=dict)
    token_source: str | None = None
    token_sources: list[dict[str, Any]] = Field(default_factory=list)
    token_field_sources: dict[str, str] = Field(default_factory=dict)
    token_coverage: dict[str, Any] | None = None
    token_scope: dict[str, Any] | None = None
    token_diagnostics: list[str] = Field(default_factory=list)
    token_evidence: dict[str, Any] | None = None
    tokens_available: bool | None = None
    tokens_complete: bool | None = None
    timing_source: str | None = None
    total_latency_available: bool | None = None
    total_latency_complete: bool | None = None
    first_response_available: bool | None = None
    first_response_complete: bool | None = None
    timing_complete: bool | None = None
    turn_timing_complete: bool | None = None
    timing_coverage: dict[str, Any] | None = None
    timing_diagnostics: list[str] = Field(default_factory=list)
    window: dict[str, Any] | None = None


class FornaxAuthResponse(BaseModel):
    status: Literal["loaded"]
    action: Literal["reload", "refresh", "update"]
    upstream_verified: bool = False


class ExportTaskRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    components: dict[str, Any] = Field(default_factory=dict)
    data_manifest_file: str | None = None
    contract_errors: list[dict[str, Any]] | None = None


class ExportJobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str | None = None
    job_id: str
    status: str
    created_at: int | None = None
    completed_count: int = 0
    task_count: int = 0
    components: list[str] = Field(default_factory=list)
    tasks: dict[str, ExportTaskRecord] = Field(default_factory=dict)
    tasks_total: int | None = None
    tasks_offset: int | None = None
    tasks_limit: int | None = None
    has_more_tasks: bool | None = None
    bytes_written: int = 0
    archive_ready: bool = False
    error: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    retry_of: dict[str, Any] | None = None
    warnings: list[str] | None = None
    finished_at: int | None = None
