"""Select independent project stores for a globally registered MCP server."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

from callmap.store import GraphStore

TEST_EXCLUDES = [
    "tests/**",
    "**/tests/**",
    "test/**",
    "**/test/**",
    "test_*.py",
    "**/test_*.py",
    "*_test.py",
    "**/*_test.py",
]


def default_cache_root() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "callmap" / "projects"


class ProjectStores:
    """Use explicit paths, never shared mutable 'current project' state."""

    def __init__(
        self,
        default_store: GraphStore | None = None,
        cache_root: str | Path | None = None,
        source_roots: list[str] | None = None,
        excludes: list[str] | None = None,
        library_roots: list[str] | None = None,
        jedi: bool = False,
        scan_mode: str = "git",
    ):
        self.default_store = default_store
        self.cache_root = (
            Path(cache_root or default_cache_root()).expanduser().resolve()
        )
        self.source_roots = source_roots
        self.excludes = excludes
        self.library_roots = library_roots
        self.jedi = jedi
        self.scan_mode = scan_mode
        self._stores: OrderedDict[tuple, GraphStore] = OrderedDict()
        self._lock = threading.RLock()

    def get(
        self,
        project_path: str | None = None,
        *,
        excludes: list[str] | None = None,
        exclude_tests: bool = False,
        scan_mode: str | None = None,
    ) -> GraphStore:
        if scan_mode is not None and scan_mode not in {"git", "filesystem"}:
            raise ValueError("scan_mode 必须为 git 或 filesystem")
        if project_path is None:
            if self.default_store is not None:
                project_path = str(self.default_store.root)
            else:
                raise ValueError(
                    "请传入 project_path：当前待分析项目的绝对路径；"
                    "全局 MCP 不使用服务进程的工作目录作为项目。"
                )
        path = Path(project_path).expanduser()
        if not project_path.strip() or not path.is_absolute():
            raise ValueError("project_path 必须是待分析项目的绝对路径")
        root = path.resolve()
        if not root.is_dir():
            raise ValueError(f"项目目录不存在: {root}")
        if (
            self.default_store is not None
            and root == self.default_store.root
            and excludes is None
            and not exclude_tests
            and scan_mode is None
        ):
            return self.default_store
        effective_excludes = (
            list(self.excludes or []) if excludes is None else list(excludes)
        )
        mode = scan_mode or self.scan_mode
        sources, libraries, jedi = self.source_roots, self.library_roots, self.jedi
        if self.default_store is not None and root == self.default_store.root:
            if excludes is None:
                effective_excludes = list(self.default_store.excludes)
            mode = scan_mode or self.default_store.scan_mode
            sources = [
                str(p.relative_to(root)) for p in self.default_store.source_roots
            ]
            libraries = [str(p) for p in self.default_store.library_roots]
            jedi = self.default_store.jedi
        if mode not in {"git", "filesystem"}:
            raise ValueError("scan_mode 必须为 git 或 filesystem")
        if exclude_tests:
            effective_excludes += TEST_EXCLUDES
        effective_excludes = sorted(set(effective_excludes))
        options = {
            "sources": sources,
            "excludes": effective_excludes,
            "libraries": libraries,
            "jedi": jedi,
            "scan_mode": mode,
        }
        options_key = hashlib.sha256(
            json.dumps(options, sort_keys=True).encode()
        ).hexdigest()[:16]
        store_key = (root, options_key)
        with self._lock:
            store = self._stores.get(store_key)
            if store is None:
                key = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
                cache = self.cache_root / key
                if sources or effective_excludes or libraries or jedi or mode != "git":
                    cache /= options_key
                store = GraphStore(
                    root,
                    cache,
                    sources,
                    effective_excludes,
                    libraries,
                    jedi,
                    mode,
                )
                self._stores[store_key] = store
                if len(self._stores) > 32:
                    self._stores.popitem(last=False)
            self._stores.move_to_end(store_key)
            return store
