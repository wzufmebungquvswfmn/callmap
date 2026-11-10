"""The registry is the allowlist and JSON Schema for editable task tags.

To add a tag, define a strict model and register it in TAG_MODELS. Both the HTTP
validator and discovery schema are generated from that same registry at startup.
"""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    field_validator,
)

from .repositories import repository_identity

SCHEMA_VERSION = "flux.dynamic-tags.v1"
Nonempty = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
RepositorySource = Literal[
    "platform_field",
    "user_specified",
    "runtime_context",
    "commit_metadata",
    "tool_execution",
]
RepositoryPurpose = Literal[
    "target_code",
    "reference_code",
    "skill_knowledge",
    "artifact_output",
    "execution_context",
    "unknown",
]
RepositoryState = Literal[
    "registered",
    "mentioned",
    "mounted",
    "commit_recorded",
    "prepare_attempted",
    "prepare_failed",
    "prepared",
    "access_attempted",
    "access_failed",
    "remote_verified",
    "read_observed",
    "modified_observed",
    "unknown",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class TagMetadata(StrictModel):
    source: Literal["human", "model", "rule"] = Field(
        description="Label supplied by the caller; not an authenticated identity."
    )
    annotator: Nonempty | None = None
    note: str = Field(default="", max_length=4000)


class RepositoryEvidence(StrictModel):
    dataset: Literal[
        "task", "execution", "conversation", "artifacts", "versions", "annotation"
    ]
    path: str = Field(min_length=1, max_length=2048)
    pointer: str = Field(
        default="", max_length=2048, pattern=r"^(?:/(?:[^~]|~[01])*)?$"
    )
    category: RepositorySource
    state: RepositoryState
    purposes: list[RepositoryPurpose] = Field(
        default_factory=lambda: ["unknown"], max_length=6
    )
    platform: str | None = Field(default=None, max_length=256)
    excerpt: str | None = Field(default=None, max_length=8192)
    result_excerpt: str | None = Field(default=None, max_length=8192)
    command: str | None = Field(default=None, max_length=8192)
    branch: str | None = Field(default=None, max_length=1024)
    revision: str | None = Field(default=None, max_length=256)
    rule: str | None = Field(default=None, max_length=256)
    encoding: Literal["json-string"] | None = None
    event_id: str | None = Field(default=None, max_length=512)
    session_id: str | None = Field(default=None, max_length=512)
    turn_id: str | None = Field(default=None, max_length=512)
    call_id: str | None = Field(default=None, max_length=512)
    trace_id: str | None = Field(default=None, max_length=512)
    span_id: str | None = Field(default=None, max_length=512)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        if (
            value.startswith("/")
            or "\\" in value
            or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("Evidence path must be task-relative without traversal")
        return value


class RepositoryLabel(StrictModel):
    name: Nonempty
    platform: str | None = Field(default=None, max_length=256)
    categories: list[RepositorySource] = Field(min_length=1, max_length=5)
    purposes: list[RepositoryPurpose] = Field(min_length=1, max_length=6)
    states: list[RepositoryState] = Field(min_length=1, max_length=13)
    evidence: list[RepositoryEvidence] = Field(default_factory=list, max_length=200)
    association: Literal["candidate", "observed"] = "candidate"
    identity_resolution: Literal["single_observed_host_for_name"] | None = None

    @field_validator("name")
    @classmethod
    def canonical_name(cls, value: str) -> str:
        identity = repository_identity(value)
        if identity != (value, None):
            raise ValueError("Use a canonical namespace/repository name, not a URL")
        return value

    @field_validator("categories", "purposes", "states")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Classification values must not repeat")
        return value


class RepositoryTag(TagMetadata):
    """Repository identities, source categories, purposes and evidence."""

    repositories: list[RepositoryLabel] = Field(max_length=200)

    @field_validator("repositories")
    @classmethod
    def unique_repositories(cls, value: list[RepositoryLabel]) -> list[RepositoryLabel]:
        identities = [(repo.name, repo.platform) for repo in value]
        if len(set(identities)) != len(identities):
            raise ValueError("Repository name/platform pairs must not repeat")
        return value


class TaskClassificationTag(TagMetadata):
    """Human/model task classification; distinct from upstream classification."""

    category: Nonempty
    subcategory: Nonempty | None = None
    reason: str = Field(default="", max_length=4000)


class ReviewTag(TagMetadata):
    """A review opinion, not a rewrite of the automatic admission decision."""

    decision: Literal["keep", "exclude", "hold"]
    reason: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]


TAG_MODELS: dict[str, type[BaseModel]] = {
    "repository": RepositoryTag,
    "task_classification": TaskClassificationTag,
    "review": ReviewTag,
}


def _absent_tag():
    # Missing properties are allowed; explicit null still fails the model type.
    return None


DynamicTaskTags = create_model(
    "DynamicTaskTags",
    __base__=StrictModel,
    __doc__="Complete replacement of editable tags. Missing tags are deleted.",
    **{
        name: (model, Field(default_factory=_absent_tag))
        for name, model in TAG_MODELS.items()
    },
)


def initial_tags(recorded: dict) -> dict:
    """Seed the editable view from the original rule output, without rewriting it."""
    tags = {}
    repository = recorded.get("repository")
    extraction = repository.get("extraction") if isinstance(repository, dict) else None
    if isinstance(extraction, dict):
        tags["repository"] = {
            "source": "rule",
            "repositories": extraction.get("repositories", []),
        }
    platform = recorded.get("platform")
    classification = (
        platform.get("task_classification") if isinstance(platform, dict) else None
    )
    if isinstance(classification, dict) and classification.get("category"):
        tags["task_classification"] = {
            "source": "rule",
            "category": classification["category"],
        }
    supported = {}
    for name, value in tags.items():
        try:
            supported[name] = (
                TAG_MODELS[name]
                .model_validate(value)
                .model_dump(mode="json", exclude_unset=True)
            )
        except ValidationError:
            # Older analyzer output can exceed this edit schema. Keep the original
            # readable in recorded_only, without inventing a lossy editable value.
            continue
    return supported
