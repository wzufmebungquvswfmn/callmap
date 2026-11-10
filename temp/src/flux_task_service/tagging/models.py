"""Serializable evidence and explicit admission policy for engineering-only tags."""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

TestType = Literal["unit", "api", "gui", "integration", "performance", "other"]


class CommandRule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    prefix: list[str] = Field(min_length=1)
    test_type: TestType

    @field_validator("prefix")
    @classmethod
    def nonempty_tokens(cls, value: list[str]) -> list[str]:
        if any(not token or token.isspace() for token in value):
            raise ValueError("Command prefix must contain nonempty tokens")
        return value


class TaggingPolicy(BaseModel):
    """Auxiliary tags deliberately have no admission thresholds."""

    model_config = ConfigDict(strict=True, extra="forbid")

    require_problem_signal: bool = False
    priority_only: bool = False
    allowed_test_types: list[TestType] = Field(
        default_factory=lambda: [
            "unit",
            "api",
            "gui",
            "integration",
            "performance",
            "other",
        ]
    )
    command_rules: list[CommandRule] = Field(default_factory=list)
    test_skills: list[str] = Field(
        default_factory=lambda: [
            "bits-unit-test-gen",
            "flux-api-test",
            "flux-sdt",
            "bits-code-guard",
            "bits-code-guard-doubao",
        ]
    )
    max_artifact_bytes: int = Field(default=5_000_000, gt=0)


class TaggingJobResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str | None = None
    tagging_id: str
    export_job_id: str | None = None
    status: str
    created_at: int | None = None
    completed_count: int = 0
    task_count: int = 0
    decision_counts: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    policy: dict[str, Any] = Field(default_factory=dict)
    allow_legacy: bool = False
    archive_ready: bool = False
    error: dict[str, Any] | None = None
    finished_at: int | None = None


class TaggingResultsPage(BaseModel):
    tagging_id: str
    total: int
    offset: int
    limit: int
    records: list[dict[str, Any]]


@dataclass
class Evidence:
    dataset: str
    path: str
    pointer: str
    event_id: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    call_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    rule: str | None = None
    excerpt: str | None = None


@dataclass
class Event:
    kind: str
    name: str
    arguments: Any
    output: Any
    status: str | None
    evidence: Evidence
    exit_code: int | None = None
    output_truncated: bool = False


@dataclass
class TestRun:
    run_id: str
    test_type: str
    origin: str
    association: str
    execution_state: str
    counts: dict[str, int | None]
    reported_issue_count: int | None
    evidence: list[Evidence]
    command: str | None = None
    versions: dict[str, Any] = field(default_factory=dict)
    invocation_status: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)
