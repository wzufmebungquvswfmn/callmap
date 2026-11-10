"""Stream a task subset from one already published export archive."""

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from flux_task_service.errors import ServiceError


def _json(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode()


def subset_archive(source: Path, task_ids: list[str]) -> Path:
    temporary = None
    try:
        # Hold one ZIP snapshot even if a retry replaces the source archive.
        with zipfile.ZipFile(source) as original:
            job = json.loads(original.read("job.json"))
            records = job["tasks"]
            selected = set(task_ids)
            if not selected <= records.keys():
                raise ServiceError(
                    "EXPORT_TASK_NOT_FOUND", "Task is not in the export archive", 404
                )
            ids = [task_id for task_id in records if task_id in selected]
            members, names, found = [], set(), set()
            for member in original.infolist():
                parts = member.filename.split("/")
                if len(parts) < 3 or parts[0] != "tasks" or parts[1] not in selected:
                    continue
                if member.is_dir():
                    continue
                if (
                    "\\" in member.filename
                    or any(part in {"", ".", ".."} for part in parts)
                    or member.filename in names
                ):
                    raise ValueError("Invalid or duplicate task member")
                names.add(member.filename)
                found.add(parts[1])
                members.append(member)
            if found != selected:
                raise ValueError("Missing selected task files")
            job["tasks"] = {task_id: records[task_id] for task_id in ids}
            job["task_count"] = len(ids)
            job["completed_count"] = sum(
                record["status"] in {"ok", "partial", "error", "cancelled"}
                for record in job["tasks"].values()
            )
            job["download_selection"] = {
                "task_ids": ids,
                "source_task_count": len(records),
            }
            if isinstance(job.get("request"), dict):
                job["request"] = {**job["request"], "task_ids": ids, "filters": None}
            if isinstance(job.get("retry_request"), dict):
                retried = [
                    task_id
                    for task_id in job["retry_request"].get("task_ids", [])
                    if task_id in selected
                ]
                if retried:
                    job["retry_request"] = {
                        **job["retry_request"],
                        "task_ids": retried,
                        "filters": None,
                    }
                else:
                    job.pop("retry_request")
            if isinstance(job.get("task_requests"), dict):
                job["task_requests"] = {
                    task_id: value
                    for task_id, value in job["task_requests"].items()
                    if task_id in selected
                }
            metadata = {"task_ids.json": _json(ids)}
            if "selection.json" in original.namelist():
                selection = json.loads(original.read("selection.json"))
                selection["source_count"] = selection["count"]
                selection.update(task_ids=ids, count=len(ids))
                metadata["selection.json"] = _json(selection)
            job["bytes_written"] = sum(member.file_size for member in members) + sum(
                len(value) for value in metadata.values()
            )
            metadata["job.json"] = _json(job)
            # Keep downloads outside export roots so repackaging cannot include them.
            with tempfile.NamedTemporaryFile(
                prefix="flux-export-", suffix=".zip", delete=False
            ) as file:
                temporary = Path(file.name)
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, value in metadata.items():
                    archive.writestr(name, value)
                for member in members:
                    with (
                        original.open(member) as incoming,
                        archive.open(member, "w", force_zip64=True) as outgoing,
                    ):
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
        return temporary
    except Exception as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if isinstance(exc, ServiceError):
            raise
        raise ServiceError(
            "EXPORT_DOWNLOAD_FAILED", "Cannot build the selected export archive", 503
        ) from exc
