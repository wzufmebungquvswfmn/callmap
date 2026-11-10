"""Immutable query scopes shared by MCP tools; descriptors hold no graph data."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path


def git_commit(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


class AnalysisContexts:
    def __init__(self, projects):
        self.projects = projects
        self.values = OrderedDict()
        self.lock = threading.RLock()

    def resolve(
        self,
        project_path=None,
        excludes=None,
        exclude_tests=None,
        scan_mode=None,
        context_entry=None,
        analysis_context=None,
    ):
        if project_path is not None and not Path(project_path).is_absolute():
            raise ValueError("project_path must be absolute")
        with self.lock:
            expected = (
                deepcopy(self.values.get(analysis_context))
                if analysis_context
                else None
            )
        if analysis_context and expected is None:
            raise ValueError("analysis context unknown/expired; open a new context")
        if expected:
            if (
                project_path
                and str(Path(project_path).resolve()) != expected["project_path"]
            ):
                raise ValueError("analysis context conflicts with project_path")
            for name, supplied in (
                ("excludes", excludes),
                ("exclude_tests", exclude_tests),
                ("scan_mode", scan_mode),
                ("context_entry", context_entry),
            ):
                if supplied is not None and supplied != expected[name]:
                    raise ValueError("analysis context conflicts with " + name)
            project_path = expected["project_path"]
            excludes, exclude_tests, scan_mode = (
                expected["excludes"],
                expected["exclude_tests"],
                expected["scan_mode"],
            )
            context_entry = expected["context_entry"]
        default = self.projects.default_store
        if (
            expected
            and default is not None
            and str(default.cache) == expected["cache_dir"]
        ):
            selected = default
        else:
            selected = self.projects.get(
                project_path,
                excludes=excludes,
                exclude_tests=bool(exclude_tests),
                scan_mode=scan_mode,
            )
        spec = {
            "project_path": str(selected.root),
            "excludes": list(selected.excludes),
            "exclude_tests": bool(exclude_tests),
            "scan_mode": selected.scan_mode,
            "cache_dir": str(selected.cache),
            "context_entry": context_entry,
            "effective_excludes": list(selected.excludes),
            "source_roots": [str(p) for p in selected.source_roots],
            "library_roots": [str(p) for p in selected.library_roots],
            "jedi": selected.jedi,
        }
        if expected and any(spec[k] != expected[k] for k in spec):
            raise ValueError("analysis context expired: scan configuration changed")
        return selected, context_entry, spec, expected

    def bind(self, selected, spec, data, expected=None):
        descriptor = spec | {
            "snapshot": data["snapshot"],
            "analysis_fingerprint": data["analysis_fingerprint"],
            "git_commit": git_commit(selected.root),
        }
        if expected and descriptor != expected:
            raise ValueError(
                "analysis context expired: source/environment/annotations/Git changed; "
                "open a new context"
            )
        token = (
            "ctx_"
            + hashlib.sha256(
                json.dumps(descriptor, sort_keys=True).encode()
            ).hexdigest()[:24]
        )
        with self.lock:
            self.values[token] = deepcopy(descriptor)
            self.values.move_to_end(token)
            if len(self.values) > 128:
                self.values.popitem(last=False)
        return token

    def describe(self, token):
        with self.lock:
            return {
                k: deepcopy(v)
                for k, v in self.values[token].items()
                if k != "cache_dir"
            }

    def scope_queries(self, data, token):
        if isinstance(data, dict):
            for key, value in data.items():
                if key == "next_query" and isinstance(value, dict):
                    value["arguments"] = {
                        k: v
                        for k, v in value["arguments"].items()
                        if k
                        not in {
                            "project_path",
                            "excludes",
                            "scan_mode",
                            "context_entry",
                            "exclude_tests",
                        }
                    }
                    value["arguments"]["analysis_context"] = token
                elif key != "analysis_scope":
                    self.scope_queries(value, token)
        elif isinstance(data, list):
            for value in data:
                self.scope_queries(value, token)
