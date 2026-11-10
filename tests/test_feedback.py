import asyncio
import json
import subprocess
import sys

import pytest

from callmap.projects import ProjectStores
from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def project(tmp_path, source):
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(source)
    return GraphStore(root, tmp_path / "cache")


def targets(store, symbol):
    graph = store.graph()
    source = store.select(graph, symbol)["id"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    return [(nodes[e["target"]], e) for e in graph["edges"] if e["source"] == source]


def test_lambda_auto_auth_callbacks_and_closures(tmp_path):
    store = project(
        tmp_path,
        """
def collect(token): pass
def wrap(manager, call):
    token = manager.get()
    call(token)
    call(manager.refresh())
class Manager:
    def get(self): return 'token'
    def refresh(self): return 'new_token'
def entry():
    wrap(Manager(), lambda token: collect(token))
""",
    )
    edges = targets(store, "wrap")
    callbacks = [(n, e) for n, e in edges if n["symbol_type"] == "lambda"]
    assert len(callbacks) == 2
    assert callbacks[0][0]["id"] == callbacks[1][0]["id"]
    assert all(e["status"] == "candidate" for _, e in callbacks)
    assert all(e["evidence"][0]["parameter"] == "call" for _, e in callbacks)
    assert any(
        n["qualified_name"] == "main.collect"
        for n, _ in targets(store, callbacks[0][0]["id"])
    )
    assert not any(
        n["qualified_name"] == "main.collect" for n, _ in targets(store, "entry")
    )


def test_executor_map_submit_and_unknown_callback(tmp_path):
    store = project(
        tmp_path,
        """
from concurrent.futures import ThreadPoolExecutor
def collect(value): pass
def run_one(value): collect(value)
def entry(callback):
    with ThreadPoolExecutor() as pool:
        pool.map(run_one, [1, 2])
        pool.submit(lambda value: collect(value), 3)
        pool.map(callback, [1])
""",
    )
    edges = targets(store, "entry")
    scheduled = [(n, e) for n, e in edges if e["reason"] == "executor-callback"]
    assert {n["symbol_type"] for n, _ in scheduled} == {"function", "lambda", "unknown"}
    assert all(e["synthetic"] for _, e in scheduled)
    assert any(
        e["status"] == "unresolved" and e["evidence"][0]["argument"] == "callback"
        for _, e in scheduled
    )
    assert any(n["qualified_name"] == "main.run_one" for n, _ in scheduled)
    assert "main.collect" in {
        n["qualified_name"] for n in store.expand("entry")["nodes"]
    }


def test_entry_context_separates_callbacks_and_fake_managers(tmp_path):
    store = project(
        tmp_path,
        """
class Base:
    def auth(self): pass
class Real(Base):
    def auth(self): pass
def wrap(manager: Base, call):
    manager.auth()
    call('token')
def collect_metrics(token): pass
def collect_traces(token): pass
def metrics(): wrap(Real(), lambda token: collect_metrics(token))
def traces(): wrap(Real(), lambda token: collect_traces(token))
""",
    )
    tests = store.root / "tests"
    tests.mkdir()
    (tests / "test_client.py").write_text("""
from main import Base, wrap
class Fake(Base):
    def auth(self): pass
def fake_callback(token): pass
def test_call(): wrap(Fake(), fake_callback)
""")
    edges = targets(store, "wrap")
    callback_edges = [(n, e) for n, e in edges if e["expression"] == "call"]
    assert len(callback_edges) == 3
    for node, edge in callback_edges:
        assert edge["evidence"]
        assert all(item["value_id"] == node["id"] for item in edge["evidence"])
        assert len({item["caller_id"] for item in edge["evidence"]}) == 1
    full = store.expand("metrics")
    assert "main.collect_traces" not in {n["qualified_name"] for n in full["nodes"]}
    wrapper = next(n for n in full["nodes"] if n["qualified_name"] == "main.wrap")
    assert wrapper["callback_candidate_count"] == 2
    narrowed = store.expand("metrics", context_entry="metrics")
    names = {n["qualified_name"] for n in narrowed["nodes"]}
    assert "main.collect_metrics" in names
    assert "main.collect_traces" not in names
    assert "tests.test_client.Fake.auth" not in names
    assert "main.Real.auth" in names
    assert not narrowed["inference_incomplete"]
    for edge in narrowed["edges"]:
        assert not any(
            item.get("caller", "").endswith("traces") for item in edge["evidence"]
        )
    filtered = ProjectStores(cache_root=tmp_path / "global").get(
        str(store.root), exclude_tests=True
    )
    assert not filtered.search("Fake")
    assert any(
        item["path"] == "tests" and item["reason"] == "configured_exclude"
        for item in filtered.stats()["coverage"]["skipped_directories"]
    )


def test_compact_budget_pagination_and_graph_limit_are_independent(tmp_path):
    store = project(
        tmp_path,
        "\n".join(
            [f"def leaf_{i}(): pass" for i in range(40)]
            + ["def entry():"]
            + [f"    leaf_{i}()" for i in range(40)]
        ),
    )
    graph = store.expand("entry", depth=1)
    assert not graph["graph_truncated"]
    for node in graph["nodes"]:
        node["doc"] = "VERY_LONG_DOC" * 10000
    query = {"symbol": "entry", "budget": 4000}
    page = bounded_graph(graph, query, budget=4000)
    assert page["response_truncated"] and not page["graph_truncated"]
    all_edges = set()
    while True:
        text = serialize(page)
        assert len(text) <= 4000
        assert page["response_characters"] == len(text)
        assert "VERY_LONG_DOC" not in text
        ids = {n["id"] for n in page["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in page["edges"])
        all_edges.update(e["id"] for e in page["edges"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > page["cursor"]
        continuation = page["continuation"]["arguments"]
        page = bounded_graph(
            graph,
            query,
            budget=4000,
            cursor=continuation["cursor"],
            snapshot=continuation["snapshot"],
        )
    assert all_edges == {e["id"] for e in graph["edges"]}
    limited = bounded_graph(store.expand("entry", max_nodes=1), query, budget=4000)
    assert limited["graph_truncated"] and not limited["response_truncated"]
    explicit_docs = bounded_graph(graph, query, budget=4000, include_docs=True)
    assert any(n.get("content_omitted") == ["doc"] for n in explicit_docs["nodes"])


def test_scan_audit_explains_git_ignored_and_nested_subprojects(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "main.py").write_text("def entry(): pass\n")
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "ignored.py").write_text("def ignored(): pass\n")
    child = root / "subproject"
    child.mkdir()
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    (child / "worker.py").write_text("def process_evaluation(): pass\n")
    git_store = GraphStore(root, tmp_path / "git-cache")
    stats = git_store.stats(file_limit=1)
    assert stats["files"] == 1
    assert not git_store.search("process_evaluation")
    coverage = git_store.ensure()["coverage"]
    reasons = {item["path"]: item["reason"] for item in coverage["skipped_files"]}
    assert reasons["ignored.py"] == "git_ignore"
    assert reasons["subproject/worker.py"] == "nested_repository_not_listed_by_git"
    assert stats["coverage"]["workspace_coverage"] == "selected_python_files_only"
    assert stats["coverage"]["next_offset"] == 1
    assert git_store.stats(file_offset=1, file_limit=1)["coverage"]["skipped_files"]
    disk_store = GraphStore(root, tmp_path / "disk-cache", scan_mode="filesystem")
    assert disk_store.search("process_evaluation")
    assert disk_store.search("ignored")
    assert disk_store.stats()["files"] == 3


def test_internal_priority_preserves_deeper_project_nodes(tmp_path):
    store = project(
        tmp_path,
        """
import os
def leaf(): pass
def branch(): leaf()
def entry():
    os.getcwd()
    os.getpid()
    branch()
""",
    )
    graph = store.expand("entry", max_nodes=3, prefer_internal=True)
    assert {n["qualified_name"] for n in graph["nodes"]} == {
        "main.entry",
        "main.branch",
        "main.leaf",
    }
    hidden = store.expand("entry", internal_only=True)
    assert all(n["kind"] != "external" for n in hidden["nodes"])


def test_callable_instances_and_submit_argument_propagation(tmp_path):
    store = project(
        tmp_path,
        """
from concurrent.futures import ThreadPoolExecutor
class Client:
    def send(self): pass
class Handler:
    def __call__(self, client): client.send()
def worker(client): client.send()
def entry():
    pool = ThreadPoolExecutor()
    pool.submit(worker, Client())
    pool.map(Handler(), [Client()])
    pool.map(0, [Client()])
class Custom:
    def map(self, callback, values): pass
def unrelated(): Custom().map(worker, [])
""",
    )
    assert {n["qualified_name"] for n, _ in targets(store, "worker")} == {
        "main.Client.send"
    }
    assert {n["qualified_name"] for n, _ in targets(store, "Handler.__call__")} == {
        "main.Client.send"
    }
    assert any(
        e["reason"] == "executor-callback" and n["kind"] == "unresolved"
        for n, e in targets(store, "entry")
    )
    assert not any(
        e["reason"] == "executor-callback" for _, e in targets(store, "unrelated")
    )


def test_lambda_closure_forwarding_and_context_recursion(tmp_path):
    store = project(
        tmp_path,
        """
def collect(token): pass
def helper(call): call('token')
def forward(call): helper(call)
def entry():
    callback = lambda token: collect(token)
    forward(callback)
    recurse(1)
def recurse(n):
    if n: recurse(n - 1)
""",
    )
    graph = store.expand("entry", context_entry="entry")
    assert "main.collect" in {n["qualified_name"] for n in graph["nodes"]}
    assert any(e["source"] == e["target"] for e in graph["edges"])
    assert not graph["inference_incomplete"]
    unreachable = store.expand("entry", context_entry="recurse")
    assert not unreachable["context_entry_reaches_symbol"]
    assert not unreachable["edges"]


def test_unknown_higher_order_argument_keeps_observed_origin(tmp_path):
    store = project(
        tmp_path,
        """
def wrap(call): call('token')
def get_callback(): pass
def entry(): wrap(get_callback())
""",
    )
    node, edge = targets(store, "wrap")[0]
    assert node["kind"] == "unresolved"
    assert edge["evidence"][0]["kind"] == "observed-argument"
    assert edge["evidence"][0]["argument"] == "get_callback()"
    assert edge["evidence"][0]["caller"] == "main.entry"


def test_budget_cursor_rejects_changed_source_and_scope(tmp_path):
    store = project(tmp_path, "def leaf(): pass\ndef entry(): leaf()\n")
    graph = store.expand("entry")
    page = bounded_graph(graph, {}, budget=4000)
    (store.root / "main.py").write_text("def entry(): pass\n")
    with pytest.raises(ValueError, match="快照"):
        bounded_graph(
            store.expand("entry"), {}, budget=4000, snapshot=page["response_snapshot"]
        )
    with pytest.raises(ValueError, match="预算"):
        bounded_graph(graph, {}, budget=1000)


@pytest.mark.parametrize("compact", [True, False])
def test_budget_cursor_rejects_changed_annotation_content(tmp_path, compact):
    store = project(
        tmp_path,
        "\n".join(
            [f"def leaf_{i}(): pass" for i in range(20)]
            + ["def entry():"]
            + [f"    leaf_{i}()" for i in range(20)]
        ),
    )
    store.annotate("entry", "OLD DOC")
    page = bounded_graph(
        store.expand("entry"), {}, budget=4000, compact=compact, include_docs=True
    )
    continuation = page["continuation"]["arguments"]
    # A new store reloads the persisted snapshot and annotations without changing
    # the continuation; a doc-only edit must then invalidate the same cursor.
    restarted = GraphStore(store.root, store.cache)
    following = bounded_graph(
        restarted.expand("entry"),
        {},
        budget=4000,
        compact=compact,
        include_docs=True,
        cursor=continuation["cursor"],
        snapshot=continuation["snapshot"],
    )
    assert following["response_snapshot"] == page["response_snapshot"]
    restarted.annotate("entry", "NEW DOC")
    with pytest.raises(ValueError, match="快照"):
        bounded_graph(
            restarted.expand("entry"),
            {},
            budget=4000,
            compact=compact,
            include_docs=True,
            cursor=continuation["cursor"],
            snapshot=continuation["snapshot"],
        )


def test_requested_docs_defer_whole_blocks_when_first_edge_would_not_fit(tmp_path):
    store = project(tmp_path, "def leaf(): pass\ndef entry(): leaf()\n")
    graph = store.expand("entry")
    for node in graph["nodes"]:
        node["doc"] = "D" * 7200
    page = bounded_graph(graph, {}, budget=20000, include_docs=True)
    assert page["edges"]
    assert len(serialize(page)) <= 20000
    # A tighter budget still yields a usable graph instead of cutting doc text.
    tight = bounded_graph(graph, {}, budget=16000, include_docs=True)
    assert tight["edges"]
    assert any(n.get("content_omitted") == ["doc"] for n in tight["nodes"])


def test_mcp_returns_one_bounded_text_block_and_preserves_page_filters(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    store = project(
        tmp_path,
        "\n".join(
            [f"def leaf_{i}(): pass" for i in range(12)]
            + ["def entry():"]
            + [f"    leaf_{i}()" for i in range(12)]
        ),
    )
    tests = store.root / "tests"
    tests.mkdir()
    (tests / "test_fake.py").write_text("def fake(): pass\n")

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "callmap", "--cache-dir", str(tmp_path / "global"), "mcp"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                args = {
                    "symbol": "entry",
                    "project_path": str(store.root),
                    "depth": 2,
                    "budget": 5000,
                    "exclude_tests": True,
                    "context_entry": "entry",
                }
                seen = set()
                while True:
                    result = await session.call_tool("expand_calls", args)
                    assert not result.isError, result.content
                    assert len(result.content) == 1 and result.structuredContent is None
                    assert len(result.content[0].text) <= 5000
                    page = json.loads(result.content[0].text)
                    assert page["response_characters"] == len(result.content[0].text)
                    assert all(
                        "doc" not in n and "code" not in n for n in page["nodes"]
                    )
                    seen.update(e["id"] for e in page["edges"])
                    if page["continuation"] is None:
                        break
                    args = page["continuation"]["arguments"]
                    assert args["analysis_context"] == page["analysis_context"]
                    assert "exclude_tests" not in args and "context_entry" not in args
                assert len(seen) == 12
                result = await session.call_tool(
                    "index_status",
                    {
                        "project_path": str(store.root),
                        "exclude_tests": True,
                    },
                )
                coverage = json.loads(result.content[0].text)["coverage"]
                assert coverage["indexed_files"] == ["main.py"]
                assert coverage["skipped_directories"][0]["path"] == "tests"

    asyncio.run(exercise())
