"""Batch tag exported task directories and write a reviewable selection index."""

import argparse
import json
import os
import tempfile
from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from flux_task_service.export_contract import ContractError, read_json

from .analyzer import analyze_task
from .models import TaggingPolicy
from .presentation import compact_result


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def discover(root: Path, *, allow_legacy: bool) -> list[Path]:
    if root.name.isdecimal() and root.is_dir():
        return [root]
    tasks = {
        path.parent
        for path in root.rglob("data-manifest.json")
        if path.parent.name.isdecimal()
    }
    if allow_legacy:
        tasks.update(
            path.parent
            for path in root.rglob("task.json")
            if path.parent.name.isdecimal()
            and path.parent.parent.name == "tasks"
            and (path.parent.parent.parent / "job.json").is_file()
        )
    return sorted(tasks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="One task directory or export root")
    parser.add_argument(
        "--output", type=Path, required=True, help="Separate output directory"
    )
    parser.add_argument(
        "--policy", type=Path, help="Strict JSON TaggingPolicy configuration"
    )
    parser.add_argument("--priority-only", action="store_true")
    parser.add_argument("--allow-legacy", action="store_true")
    args = parser.parse_args(argv)
    root, output = args.input.resolve(), args.output.resolve()
    try:
        policy = (
            TaggingPolicy.model_validate(read_json(args.policy))
            if args.policy
            else TaggingPolicy()
        )
        if args.priority_only:
            policy = policy.model_copy(update={"priority_only": True})
        if not root.is_dir():
            raise ValueError("Input must be an extracted directory")
        if output == root or output.is_relative_to(root) or root.is_relative_to(output):
            raise ValueError("Output and input directories must not contain each other")
        paths = discover(root, allow_legacy=args.allow_legacy)
        if not paths:
            raise ValueError(
                "No task manifests found; legacy adaptation requires --allow-legacy"
            )
        if len({path.name for path in paths}) != len(paths):
            raise ValueError(
                "Duplicate task IDs across exports; analyze snapshots separately"
            )
    except (ContractError, ValidationError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    records = []
    failed = False
    for path in paths:
        try:
            result = analyze_task(path, policy=policy, allow_legacy=args.allow_legacy)
        except ContractError as exc:
            failed = True
            result = {
                "task_id": path.name,
                "admission": {
                    "candidate_level": "review",
                    "decision": "hold",
                    "reason_codes": [exc.code],
                },
                "analysis_errors": [{"code": exc.code, "message": str(exc)}],
            }
        if result.get("schema_version") != "flux.test-admission.v2":
            result = compact_result(result)
        _write(output / "tasks" / f"{path.name}.json", result)
        records.append(
            {
                "task_id": path.name,
                "input_dir": str(path),
                "result": f"tasks/{path.name}.json",
                **result["admission"],
                "tags": result["recorded_only"],
                "test_evidence_level": result.get("core", {}).get(
                    "test_evidence_level"
                ),
            }
        )
    summary = {
        "schema_version": "flux.test-admission-index.v2",
        "policy": policy.model_dump(),
        "task_count": len(records),
        "decision_counts": dict(Counter(r["decision"] for r in records)),
        "records": records,
    }
    _write(output / "index.json", summary)
    _write(
        output / "selected-tasks.json", [r for r in records if r["decision"] == "keep"]
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "task_count": len(records),
                "decision_counts": summary["decision_counts"],
            },
            ensure_ascii=False,
        )
    )
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
