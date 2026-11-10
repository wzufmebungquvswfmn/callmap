"""Git change impact using current and baseline static graphs."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import subprocess
import tarfile
from collections import deque
from pathlib import Path


def git(root, *args, text=True):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=text
    )
    if result.returncode:
        raise ValueError(
            "Git impact query failed: "
            + (result.stderr.strip() if text else "archive failed")
        )
    return result.stdout


def patch_files(patch):
    result = []
    current = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            paths = shlex.split(line)[2:]
            if len(paths) != 2:
                raise ValueError("unsupported diff file header")
            current = {
                "old_path": paths[0].removeprefix("a/"),
                "path": paths[1].removeprefix("b/"),
                "ranges": [],
                "hunks": [],
            }
            result.append(current)
        elif current and line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not match:
                raise ValueError("unsupported diff hunk")
            old, count, new, length = match.groups()
            current["ranges"].append(
                (int(old), int(count or 1), int(new), int(length or 1))
            )
            current["hunks"].append({"old": [], "new": []})
        elif current and line.startswith("--- /dev/null"):
            current["old_path"] = None
        elif current and line.startswith("+++ /dev/null"):
            current["path"] = None
        elif current and current["hunks"] and line[:1] in {" ", "+", "-"}:
            if line[0] != "+":
                current["hunks"][-1]["old"].append(line[1:])
            if line[0] != "-":
                current["hunks"][-1]["new"].append(line[1:])
    if patch.strip() and not result:
        raise ValueError("provide a Git unified diff with diff --git headers")
    return result


def materialize(store, commit):
    from callmap.store import GraphStore

    data = git(store.root, "archive", commit, text=False)
    tree = store.cache / ".callmap" / "impact" / commit / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    members = set()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            name = Path(member.name)
            if not member.isfile() or not (
                member.name.endswith(".py")
                or name.name
                in {
                    "pyproject.toml",
                    "uv.lock",
                    "poetry.lock",
                    "requirements.txt",
                    ".gitignore",
                }
            ):
                continue
            if name.is_absolute() or ".." in name.parts:
                raise ValueError("unsafe archive path")
            path = tree / name
            if not path.resolve().is_relative_to(tree.resolve()):
                raise ValueError("unsafe baseline cache path")
            content = archive.extractfile(member).read()
            expected = hashlib.sha256(content).hexdigest()
            if not path.is_file() or store._hashes.digest(path) != expected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            members.add(member.name)
    if any(p.relative_to(tree).as_posix() not in members for p in tree.rglob("*.py")):
        raise ValueError("baseline cache contains files outside the Git tree")
    key = (
        commit,
        tuple(store.excludes),
        tuple(store.source_roots),
        tuple(store.library_roots),
        store.jedi,
    )
    if not hasattr(store, "_impact_stores"):
        store._impact_stores = {}
    if key not in store._impact_stores:
        store._impact_stores[key] = GraphStore(
            tree,
            tree.parent / "index",
            [str(p.relative_to(store.root)) for p in store.source_roots],
            list(store.excludes),
            [str(p) for p in store.library_roots],
            store.jedi,
            "filesystem",
        )
        if len(store._impact_stores) > 2:
            del store._impact_stores[next(iter(store._impact_stores))]
    return store._impact_stores[key]._query_graph()


def owners(graph, path, start, length):
    candidates = [
        n
        for n in graph["nodes"]
        if n["path"] == path and n["symbol_type"] in {"function", "class", "lambda"}
    ]
    found = {}
    for line in range(max(1, start - (length == 0)), max(1, start) + max(1, length)):
        containing = [n for n in candidates if n["line"] <= line <= n["end_line"]]
        if containing:
            node = min(containing, key=lambda n: n["end_line"] - n["line"])
            found[node["identity"].split("::", 1)[1]] = node
    return found


def upstream(graph, node, maximum):
    entries = {e["id"]: e for e in graph["entry_points"]["entries"]}
    indexes = graph["_indexes"]
    visited = {node["id"]}
    queue = deque([(node["id"], [node["id"]])])
    roots, tests = [], []
    truncated = False
    while queue:
        sid, path = queue.popleft()
        current = indexes.by_id[sid]
        info = {
            "id": sid,
            "symbol": current["qualified_name"],
            "path": current["path"],
            "line": current["line"],
            "call_path": list(reversed(path)),
        }
        if sid in entries and len(roots) < 8:
            roots.append(
                info
                | {
                    "entry_kinds": entries[sid]["entry_kinds"],
                    "deployment_verified": False,
                }
            )
        if current.get("is_test") and len(tests) < 8:
            tests.append(info)
        for edge in indexes.incoming.get(sid, []):
            caller = edge["source"]
            if caller in visited:
                continue
            if len(visited) >= maximum:
                truncated = True
                continue
            if len(path) >= 12:
                truncated = True
                continue
            visited.add(caller)
            queue.append((caller, path + [caller]))
    return {
        "entries": roots,
        "tests": tests,
        "truncated": truncated,
        "visited_nodes": len(visited),
        "runtime_complete": False,
        "unresolved_index_callsites": indexes.unresolved_count,
        "test_coverage_verified": False,
    }


def impact(
    store,
    base="HEAD",
    patch=None,
    limit=20,
    offset=0,
    max_nodes=200,
    context_entry=None,
    budget=20000,
):
    if not 1 <= limit <= 200 or offset < 0 or max_nodes < 1:
        raise ValueError("invalid impact query limits")
    commit = git(
        store.root, "rev-parse", "--verify", "--end-of-options", base + "^{commit}"
    ).strip()
    current = store._query_graph(context_entry)
    baseline = materialize(store, commit)
    if patch is None:
        patch = git(
            store.root,
            "-c",
            "core.quotepath=false",
            "diff",
            "--find-renames",
            "--unified=0",
            commit,
            "--",
            "*.py",
        )
    changes = patch_files(patch)
    modules = {
        n["path"]: n["code"] for n in current["nodes"] if n["symbol_type"] == "module"
    }
    old_modules = {
        n["path"]: n["code"] for n in baseline["nodes"] if n["symbol_type"] == "module"
    }
    records, omitted = [], []
    for change in changes:
        old_path, path = change["old_path"], change["path"]
        if not (path or old_path).endswith(".py"):
            continue
        if path and path not in modules or old_path and old_path not in old_modules:
            omitted.append(
                {"path": path or old_path, "reason": "outside_indexed_scope"}
            )
            continue
        ranges = change["ranges"]
        for bounds, hunk in zip(ranges, change["hunks"]):
            old_line, old_count, new_line, new_count = bounds
            previous = old_modules.get(old_path, "").splitlines()
            present = modules.get(path, "").splitlines()
            if (
                previous[max(0, old_line - 1) : max(0, old_line - 1) + old_count]
                != hunk["old"]
                or present[max(0, new_line - 1) : max(0, new_line - 1) + new_count]
                != hunk["new"]
            ):
                raise ValueError(
                    "diff hunk does not match the Git base/current indexed source"
                )
        if not ranges and path != old_path:
            ranges = [
                (
                    1,
                    len(old_modules.get(old_path, "").splitlines()),
                    1,
                    len(modules.get(path, "").splitlines()),
                )
            ]
        before, after = {}, {}
        for old_line, old_count, new_line, new_count in ranges:
            before.update(owners(baseline, old_path, old_line, old_count))
            after.update(owners(current, path, new_line, new_count))
        if not before and not after:
            omitted.append(
                {
                    "path": path or old_path,
                    "reason": "changes_outside_callable_symbols; inspect references",
                }
            )
        for name in sorted(before.keys() | after.keys()):
            old = before.get(name) or next(
                (
                    n
                    for n in baseline["nodes"]
                    if n["path"] == old_path and n["identity"].split("::", 1)[1] == name
                ),
                None,
            )
            new = after.get(name) or next(
                (
                    n
                    for n in current["nodes"]
                    if n["path"] == path and n["identity"].split("::", 1)[1] == name
                ),
                None,
            )
            if old and new and path == old_path and old["hash"] == new["hash"]:
                continue
            records.append(
                {
                    "change": "added"
                    if old is None
                    else "removed"
                    if new is None
                    else "renamed_file"
                    if path != old_path
                    else "modified",
                    "symbol": (new or old)["qualified_name"],
                    "path": path or old_path,
                    "before": (
                        {
                            "id": old["id"],
                            "line": old["line"],
                            "source": old["code"][:1000],
                            "impact": upstream(baseline, old, max_nodes),
                        }
                        if old
                        else None
                    ),
                    "after": (
                        {
                            "id": new["id"],
                            "line": new["line"],
                            "impact": upstream(current, new, max_nodes),
                        }
                        if new
                        else None
                    ),
                }
            )
    output = {
        "result": records[offset : offset + limit],
        "total": len(records),
        "next_offset": offset + limit if offset + limit < len(records) else None,
        "omitted_files": omitted,
        "base_commit": commit,
        "baseline_scope": "tracked Git Python files; active excludes applied",
        "excluded_tests": any("test" in pattern for pattern in store.excludes),
        "test_coverage_verified": False,
        "snapshot": current["snapshot"],
        "analysis_fingerprint": current["analysis_fingerprint"],
        "project_path": str(store.root),
    }
    while output["result"] and len(json.dumps(output)) > budget:
        output["result"].pop()
    if records[offset:] and not output["result"]:
        raise ValueError("impact budget too small; increase budget")
    output["next_offset"] = (
        offset + len(output["result"])
        if offset + len(output["result"]) < len(records)
        else None
    )
    return output
