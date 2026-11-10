"""Transactional replacement of tag maps, shared by threads and processes.

Original analyzer files are immutable. SQLite is the source of truth for edits;
all HTTP views apply one committed snapshot over those original files.
"""

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from flux_task_service.errors import ServiceError

from .tag_schema import SCHEMA_VERSION


@dataclass(frozen=True)
class TagSnapshot:
    tags: dict
    revision: int = 0
    updated_at: int | None = None

    def etag(self, tagging_id: str, task_id: str) -> str:
        value = [SCHEMA_VERSION, tagging_id, task_id, self.revision, self.tags]
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return '"' + hashlib.sha256(encoded.encode()).hexdigest() + '"'

    def fields(self) -> dict:
        return {
            "dynamic_tags": self.tags,
            "dynamic_tag_revision": self.revision,
            "dynamic_tag_updated_at": self.updated_at,
        }


def _path(directory: Path) -> Path:
    path = directory / "dynamic-tags.sqlite3"
    if path.is_symlink():
        raise ServiceError("TAGGING_WRITE_FAILED", "Invalid tag storage path", 503)
    return path


def read_tags(directory: Path) -> dict[str, TagSnapshot]:
    path = _path(directory)
    if not path.exists():
        return {}
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        ) as connection:
            # A concurrent first writer may have created the DB but not the table.
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='task_tags'"
            ).fetchone()
            if table is None:
                return {}
            rows = connection.execute(
                "SELECT task_id, payload, revision, updated_at FROM task_tags"
            ).fetchall()
        return {
            task_id: TagSnapshot(json.loads(payload), revision, updated_at)
            for task_id, payload, revision, updated_at in rows
        }
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise ServiceError(
            "TAGGING_READ_FAILED", "Cannot read editable tags", 503
        ) from exc


def replace_tags(
    directory: Path,
    tagging_id: str,
    task_id: str,
    tags: dict,
    expected_etag: str | None,
    initial: dict,
) -> TagSnapshot:
    if expected_etag is None:
        raise ServiceError(
            "TAGGING_PRECONDITION_REQUIRED",
            "GET the task tags, merge locally, then POST with its ETag in If-Match",
            428,
        )
    path = _path(directory)
    try:
        with closing(sqlite3.connect(path, timeout=5)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS task_tags (
                    task_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            # Acquire the cross-process writer lock before checking the revision.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision, updated_at FROM task_tags WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            current = (
                TagSnapshot(json.loads(row[0]), row[1], row[2])
                if row
                else TagSnapshot(initial)
            )
            if expected_etag != current.etag(tagging_id, task_id):
                raise ServiceError(
                    "TAGGING_REVISION_CONFLICT",
                    "Tags changed or If-Match is invalid; GET again, merge and retry",
                    412,
                )
            updated = TagSnapshot(tags, current.revision + 1, int(time.time() * 1000))
            connection.execute(
                """INSERT INTO task_tags VALUES (?, ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   payload=excluded.payload, revision=excluded.revision,
                   updated_at=excluded.updated_at""",
                (
                    task_id,
                    json.dumps(tags, ensure_ascii=False, allow_nan=False),
                    updated.revision,
                    updated.updated_at,
                ),
            )
        return updated
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise ServiceError(
            "TAGGING_WRITE_FAILED", "Cannot save editable tags", 503
        ) from exc
