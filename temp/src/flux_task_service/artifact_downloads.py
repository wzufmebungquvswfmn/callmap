"""One manifest refresh per task and one recovery download per failed file."""

import asyncio
import hashlib
import time

from flux_task_service.errors import ServiceError
from flux_task_service.models import TaskIdentity
from flux_task_service.resources import ResourceService, public_error


def verify_content(content: bytes, item: dict) -> None:
    digest = item.get("sha256")
    if digest and hashlib.sha256(content).hexdigest().lower() != str(digest).lower():
        raise ServiceError("ARTIFACT_INTEGRITY_ERROR", "Manifest checksum mismatch")
    if item.get("size") is not None and item["size"] != len(content):
        raise ServiceError("ARTIFACT_INTEGRITY_ERROR", "Manifest size mismatch")


def recoverable(exc: ServiceError) -> bool:
    return exc.code in {
        "ARTIFACT_INTEGRITY_ERROR",
        "ARTIFACT_DOWNLOAD_FAILED",
        "UPSTREAM_UNAVAILABLE",
        "UPSTREAM_RETRIES_EXHAUSTED",
    } or (
        exc.code == "UPSTREAM_REJECTED"
        and exc.upstream_code in {1001, 3001, 3002, 9000}
    )


class ArtifactDownloads:
    def __init__(
        self, resources: ResourceService, identity: TaskIdentity, manifest: dict
    ):
        self.resources = resources
        self.identity = identity
        self.original = manifest
        self.refreshed: dict | None = None
        self.refresh_attempted = False
        self.refresh_error: dict | None = None
        self.deadline: float | None = None
        self.current = {item["path"]: item for item in manifest["files"]}

    def selected(self, path: str) -> dict | None:
        return self.current.get(path)

    def describe(self, record: dict, original: dict, item: dict) -> None:
        record["verified_against"] = "refreshed" if self.refreshed else "initial"
        record["manifest_changed"] = any(
            item.get(key) != original.get(key) for key in ("size", "sha256")
        )
        if record["manifest_changed"]:
            record["initial_expected"] = {
                key: original.get(key) for key in ("size", "sha256")
            }
            record["verified_expected"] = {
                key: item.get(key) for key in ("size", "sha256")
            }

    async def read(self, item: dict) -> bytes:
        response = await self.resources.artifact(
            self.identity, item["path"], max_retries=0
        )
        verify_content(response.content, item)
        return response.content

    async def download(self, original: dict) -> tuple[bytes | None, dict]:
        path = original["path"]
        item = self.selected(path)
        record = {
            "path": path,
            "status": "error",
            "attempts": 0,
            "verified_against": "refreshed" if self.refreshed else "initial",
        }
        if item is None:
            record["error"] = {
                "code": "ARTIFACT_REMOVED",
                "message": "Artifact is absent from the refreshed manifest",
            }
            return None, record
        self.describe(record, original, item)
        try:
            record["attempts"] += 1
            content = await self.read(item)
        except ServiceError as exc:
            record["error"] = public_error(exc)
            if not recoverable(exc):
                return None, record
            record["first_error"] = public_error(exc)
            record["recovery_attempted"] = True
            if self.deadline is None:
                self.deadline = (
                    time.monotonic()
                    + self.resources.settings.artifact_recovery_timeout_seconds
                )
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                record["error"] = {
                    "code": "ARTIFACT_RECOVERY_TIMEOUT",
                    "message": "Task artifact recovery time budget exhausted",
                }
                return None, record
            try:
                async with asyncio.timeout(remaining):
                    if not self.refresh_attempted:
                        self.refresh_attempted = True
                        try:
                            refreshed = await self.resources.artifacts(
                                self.identity, max_retries=0
                            )
                            if refreshed.get("task_id") not in (
                                None,
                                self.original.get("task_id"),
                            ):
                                raise ServiceError(
                                    "UPSTREAM_FILTER_MISMATCH",
                                    "Refreshed manifest belongs to another task",
                                )
                            self.refreshed = refreshed
                            self.current = {
                                entry["path"]: entry for entry in refreshed["files"]
                            }
                        except ServiceError as refresh_exc:
                            self.refresh_error = public_error(refresh_exc)
                    if self.refresh_error:
                        record["recovery_error"] = self.refresh_error
                        return None, record
                    item = self.selected(path)
                    if item is None:
                        record["verified_against"] = "refreshed"
                        record["error"] = {
                            "code": "ARTIFACT_REMOVED",
                            "message": "Artifact is absent from the refreshed manifest",
                        }
                        return None, record
                    self.describe(record, original, item)
                    record["attempts"] += 1
                    content = await self.read(item)
            except TimeoutError:
                record["error"] = {
                    "code": "ARTIFACT_RECOVERY_TIMEOUT",
                    "message": "Task artifact recovery time budget exhausted",
                }
                if self.refreshed is None:
                    self.refresh_error = record["error"]
                return None, record
            except ServiceError as final_exc:
                record["error"] = public_error(final_exc)
                return None, record
            record.pop("error", None)
            record["recovered"] = True
        record.update(status="ok", size=len(content))
        return content, record
