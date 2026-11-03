"""Audit Python source selection, including files omitted by Git enumeration."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    return (
        "tests" in parts
        or "test" in parts
        or Path(path).name.startswith("test_")
        or Path(path).name.endswith("_test.py")
    )


def scan_sources(
    root: Path, cache: Path, excludes: list[str], ignored: set[str], mode: str
) -> tuple[list[Path], dict]:
    git_files = None
    if mode == "git":
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "-z",
                    "--",
                    "*.py",
                ],
                capture_output=True,
                check=True,
            )
            git_files = {root / os.fsdecode(p) for p in result.stdout.split(b"\0") if p}
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    discovered = set()
    directories = []
    audit_errors = []
    nested_repositories = []

    def walk_error(error):
        audit_errors.append({"path": str(error.filename), "error": str(error)})

    for folder, children, names in os.walk(root, followlinks=False, onerror=walk_error):
        parent = Path(folder)
        for child in list(children):
            path = parent / child
            relative = path.relative_to(root).as_posix()
            reason = None
            pattern = next(
                (
                    p
                    for p in excludes
                    if p.endswith("/**") and fnmatch.fnmatch(relative + "/", p)
                ),
                None,
            )
            if path == cache or path.is_relative_to(cache):
                reason = "cache_directory"
            elif child in ignored:
                reason = "builtin_exclude"
            elif path.is_symlink():
                reason = "symlink_directory_not_followed"
            elif pattern:
                reason = "configured_exclude"
            elif child.startswith(".") and not any(
                p.is_relative_to(path) for p in git_files or []
            ):
                reason = "hidden_directory"
            if reason:
                children.remove(child)
                directories.append(
                    {"path": relative, "reason": reason}
                    | ({"pattern": pattern} if pattern else {})
                )
            elif (path / ".git").exists():
                nested_repositories.append(path)
        discovered.update(parent / name for name in names if name.endswith(".py"))
    ignored_by_git = {}
    missing = discovered - (git_files or set()) if git_files is not None else set()
    if missing:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "check-ignore",
                    "--no-index",
                    "-v",
                    "-z",
                    "--stdin",
                ],
                input=b"\0".join(
                    os.fsencode(p.relative_to(root)) for p in sorted(missing)
                )
                + b"\0",
                capture_output=True,
            )
            fields = result.stdout.split(b"\0")
            for index in range(0, len(fields) - 3, 4):
                source, line, pattern, path = map(
                    os.fsdecode, fields[index : index + 4]
                )
                if not pattern.startswith("!"):
                    ignored_by_git[root / path] = {
                        "ignore_source": source,
                        "ignore_line": line,
                        "pattern": pattern,
                    }
        except FileNotFoundError:
            pass
    candidates = discovered if git_files is None else git_files
    selected = []
    skipped = []
    for path in sorted(discovered | candidates):
        relative = path.relative_to(root).as_posix()
        reason = None
        extra = {}
        pattern = next((p for p in excludes if fnmatch.fnmatch(relative, p)), None)
        if not path.is_file():
            reason = "missing_file"
        elif not path.resolve().is_relative_to(root):
            reason = "outside_project_symlink"
        elif path.is_relative_to(cache):
            reason = "cache_directory"
        elif any(p in ignored for p in path.relative_to(root).parts):
            reason = "builtin_exclude"
        elif pattern:
            reason, extra = "configured_exclude", {"pattern": pattern}
        elif path not in candidates:
            if path in ignored_by_git:
                reason, extra = "git_ignore", ignored_by_git[path]
            else:
                nested = next(
                    (p for p in nested_repositories if path.is_relative_to(p)), None
                )
                reason = (
                    "nested_repository_not_listed_by_git"
                    if nested
                    else "not_in_git_file_list"
                )
                if nested:
                    extra = {"repository": nested.relative_to(root).as_posix()}
        if reason:
            skipped.append({"path": relative, "reason": reason} | extra)
        else:
            selected.append(path)
    return selected, {
        "enumeration": "git_file_list" if git_files is not None else "filesystem",
        "indexed_files": [],
        "skipped_files": skipped,
        "skipped_directories": directories,
        "audit_errors": audit_errors,
        "audit_complete": not audit_errors,
        "workspace_coverage": "selected_python_files_only",
        "excluded_directory_contents_enumerated": False,
        "nested_repositories": [
            p.relative_to(root).as_posix() for p in nested_repositories
        ],
    }
