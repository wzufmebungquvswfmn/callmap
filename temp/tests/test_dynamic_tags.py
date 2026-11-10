"""Typed tag maps and conditional writes are the dynamic-tag contract."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from flux_task_service.errors import ServiceError
from flux_task_service.tagging import tag_store
from flux_task_service.tagging.tag_schema import (
    TAG_MODELS,
    DynamicTaskTags,
    initial_tags,
)
from flux_task_service.tagging.tag_store import TagSnapshot, read_tags, replace_tags

TAGGING_ID = "a" * 32
REVIEW = {"source": "human", "decision": "hold", "reason": "需要核对证据"}


@pytest.mark.parametrize(
    "payload",
    [
        {"unknown": {}},
        {"review": None},
        {"review": "keep"},
        {"review": {"source": "human", "decision": "keep"}},
        {"review": {**REVIEW, "source": "robot"}},
        {"review": {**REVIEW, "decision": "approved"}},
        {"review": {**REVIEW, "reason": "  "}},
        {"review": {**REVIEW, "extra": "hidden"}},
        {"task_classification": {"source": "model", "category": 42}},
        {"repository": {"source": "model", "repositories": {}}},
        {
            "repository": {
                "source": "model",
                "repositories": [
                    {
                        "name": "https://github.com/org/repo",
                        "categories": ["user_specified"],
                        "purposes": ["target_code"],
                        "states": ["mentioned"],
                    }
                ],
            }
        },
        {
            "repository": {
                "source": "model",
                "repositories": [
                    {
                        "name": "org/repo",
                        "categories": ["guessed"],
                        "purposes": ["target_code"],
                        "states": ["mentioned"],
                    }
                ],
            }
        },
    ],
)
def test_reject_unknown_tags_nulls_types_and_nested_fields(payload):
    with pytest.raises(ValidationError):
        DynamicTaskTags.model_validate(payload)


def test_registry_generates_discoverable_schema_and_roundtrip():
    schema = DynamicTaskTags.model_json_schema()
    assert set(schema["properties"]) == set(TAG_MODELS)
    assert schema["additionalProperties"] is False
    assert DynamicTaskTags.model_validate({}).model_dump(exclude_unset=True) == {}
    payload = {"review": REVIEW}
    assert (
        DynamicTaskTags.model_validate(payload).model_dump(exclude_unset=True)
        == payload
    )


def test_incompatible_legacy_seed_does_not_block_reading_or_editing():
    recorded = {
        "repository": {"extraction": {"repositories": [{"name": "legacy-only"}]}},
        "platform": {"task_classification": {"category": "开发"}},
    }
    assert initial_tags(recorded) == {
        "task_classification": {"source": "rule", "category": "开发"}
    }
    assert recorded["repository"]["extraction"]["repositories"] == [
        {"name": "legacy-only"}
    ]


def test_two_store_writers_with_same_revision_cannot_lose_an_update(tmp_path):
    etag = TagSnapshot({}).etag(TAGGING_ID, "111")

    def write(reason):
        try:
            snapshot = replace_tags(
                tmp_path,
                TAGGING_ID,
                "111",
                {
                    "review": {**REVIEW, "reason": reason},
                },
                etag,
                {},
            )
            return snapshot.revision
        except ServiceError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["review A", "review B"]))
    assert sorted(results) == [1, 412]
    assert read_tags(tmp_path)["111"].revision == 1


def test_failed_write_rolls_back_the_payload_and_revision(tmp_path, monkeypatch):
    initial = {"review": REVIEW}
    original = replace_tags(
        tmp_path,
        TAGGING_ID,
        "111",
        initial,
        TagSnapshot({}).etag(TAGGING_ID, "111"),
        {},
    )
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            value = super().execute(sql, *args, **kwargs)
            if sql.startswith("INSERT INTO"):
                raise sqlite3.OperationalError("simulated storage failure after insert")
            return value

    monkeypatch.setattr(
        tag_store.sqlite3,
        "connect",
        lambda *a, **kw: connect(*a, **kw, factory=FailingConnection),
    )
    with pytest.raises(ServiceError) as failure:
        replace_tags(
            tmp_path, TAGGING_ID, "111", {}, original.etag(TAGGING_ID, "111"), {}
        )
    assert failure.value.status_code == 503
    assert read_tags(tmp_path)["111"] == original


def test_empty_replacement_remains_empty_and_etags_bind_task_and_revision(tmp_path):
    baseline = {"review": REVIEW}
    etag = TagSnapshot(baseline).etag(TAGGING_ID, "111")
    first = replace_tags(tmp_path, TAGGING_ID, "111", {}, etag, baseline)
    second = replace_tags(
        tmp_path, TAGGING_ID, "111", {}, first.etag(TAGGING_ID, "111"), baseline
    )
    assert read_tags(tmp_path)["111"].tags == {}
    assert first.etag(TAGGING_ID, "111") != second.etag(TAGGING_ID, "111")
    assert second.etag(TAGGING_ID, "111") != second.etag(TAGGING_ID, "222")
