import asyncio
import json
import subprocess
import sys

import pytest

from callmap.projects import ProjectStores
from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


@pytest.fixture
def store(tmp_path):
    (tmp_path / "demo.py").write_text(
        "import os.path\n"
        "def leaf(): return 1\n"
        "def left():\n    leaf()\n    leaf()\n"
        "def right(): leaf()\n"
        "def entry():\n    left()\n    right()\n"
        "def recursive():\n    recursive()\n    leaf()\n"
        "def cycle_a(): cycle_b()\n"
        "def cycle_b(): cycle_a()\n"
        "handlers = {'leaf': leaf}\n"
        "def factory(key): handlers[key]()\n"
        "def external(): os.path.join('a', 'b')\n"
        "def unknown(callback): callback()\n"
        "class Client:\n    def __init__(self): pass\n"
        "def construct(): Client()\n"
    )
    return GraphStore(tmp_path)


def test_direct_callers_preserve_callsites_and_candidate_evidence(store):
    graph = store.callers("demo.leaf")
    nodes = {n["id"]: n for n in graph["nodes"]}
    root = nodes[graph["entry"]]
    assert graph["direction"] == "upstream"
    assert {n["qualified_name"] for n in nodes.values()} == {
        "demo.leaf",
        "demo.left",
        "demo.right",
        "demo.recursive",
        "demo.factory",
    }
    incoming = [e for e in graph["edges"] if e["target"] == root["id"]]
    assert root["incoming_count"] == len(incoming) == 5
    left_edges = [e for e in incoming if nodes[e["source"]]["name"] == "left"]
    assert len(left_edges) == 2
    assert len({e["line"] for e in left_edges}) == 2
    candidate = next(e for e in incoming if nodes[e["source"]]["name"] == "factory")
    assert candidate["status"] == "candidate" and candidate["evidence"]
    assert all(n["depth"] == 1 for sid, n in nodes.items() if sid != root["id"])
    assert "code" not in root
    assert graph["truncated"]  # entry calls left/right beyond depth=1


def test_transitive_cycles_limits_and_symbol_lookup(store):
    graph = store.callers("leaf", depth=-1, include_code=True)
    assert "demo.entry" in {n["qualified_name"] for n in graph["nodes"]}
    assert not graph["truncated"]
    assert len(graph["nodes"]) == len({n["id"] for n in graph["nodes"]})
    assert any(e["source"] == e["target"] for e in graph["edges"])
    assert all("code" in n for n in graph["nodes"])
    cycle = store.callers("demo.cycle_a", depth=-1)
    assert len(cycle["nodes"]) == len(cycle["edges"]) == 2
    assert not cycle["truncated"]
    limited = store.callers(graph["entry"], depth=-1, max_nodes=1)
    assert len(limited["nodes"]) == 1 and limited["truncated"]
    assert limited["omitted_frontier_count"] == 4
    assert store.callers("demo.entry")["edges"] == []
    assert store.callers("demo.leaf", depth=0)["truncated"]
    with pytest.raises(ValueError):
        store.callers("demo.leaf", depth=-2)
    with pytest.raises(ValueError):
        store.callers("demo.leaf", max_nodes=0)
    with pytest.raises(ValueError):
        store.callers("missing")


def test_external_constructor_and_manual_targets(store):
    external = store.callers("os.path.join", internal_only=True)
    assert len(external["edges"]) == 1
    assert {n["qualified_name"] for n in external["nodes"]} == {
        "os.path.join",
        "demo.external",
    }
    constructor = store.callers("demo.Client.__init__", depth=-1)
    assert any(e["reason"] == "constructor" for e in constructor["edges"])
    assert "demo.construct" in {n["qualified_name"] for n in constructor["nodes"]}
    unknown = store.expand("demo.unknown")
    target = next(n for n in unknown["nodes"] if n["kind"] == "unresolved")
    store.annotate(target["id"], "Known callback", ["demo.leaf"])
    graph = store.callers("demo.leaf")
    assert any(e["status"] == "manual" for e in graph["edges"])


def test_scanning_filters_and_context(store):
    (store.root / "test_demo.py").write_text(
        "from demo import leaf\ndef test_leaf(): leaf()\n"
    )
    projects = ProjectStores(store, cache_root=store.root / "caches")
    full = projects.get().callers("demo.leaf")
    assert any(n["is_test"] for n in full["nodes"])
    filtered = projects.get(exclude_tests=True).callers("demo.leaf")
    assert not any(n["is_test"] for n in filtered["nodes"])
    contextual = store.callers("demo.leaf", depth=-1, context_entry="demo.entry")
    assert contextual["context_entry_reaches_symbol"]
    outside = store.callers("demo.external", context_entry="demo.entry")
    assert not outside["context_entry_reaches_symbol"]


def test_upstream_pagination_and_direction_snapshot(store):
    graph = store.callers("demo.leaf", depth=-1)
    query = {"symbol": "demo.leaf", "depth": -1, "budget": 4000}
    page = bounded_graph(graph, query, budget=4000)
    edge_ids = set()
    assert page["response_truncated"]
    while True:
        assert len(serialize(page)) <= 4000
        assert page["direction"] == "upstream"
        assert page["response_characters"] == len(serialize(page))
        ids = {n["id"] for n in page["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in page["edges"])
        edge_ids.update(e["id"] for e in page["edges"])
        if not page["continuation"]:
            break
        assert page["continuation"]["tool"] == "find_callers"
        args = page["continuation"]["arguments"]
        page = bounded_graph(
            graph, query, budget=4000, cursor=args["cursor"], snapshot=args["snapshot"]
        )
    assert edge_ids == {e["id"] for e in graph["edges"]}
    with pytest.raises(ValueError, match="快照"):
        bounded_graph(
            graph | {"direction": "downstream"},
            query,
            snapshot=page["response_snapshot"],
        )


def test_cli_upstream_exports(store, tmp_path):
    command = [sys.executable, "-m", "callmap", "--project", str(store.root)]
    result = subprocess.run(
        command + ["callers", "demo.leaf"], check=True, capture_output=True, text=True
    )
    graph = json.loads(result.stdout)
    assert graph["direction"] == "upstream"
    assert "demo.entry" not in {n["qualified_name"] for n in graph["nodes"]}
    output = tmp_path / "upstream.mmd"
    subprocess.run(
        command
        + [
            "callers",
            "demo.leaf",
            "--depth",
            "-1",
            "--format",
            "mermaid",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    assert "demo.entry" in output.read_text()
    result = subprocess.run(
        command + ["context", "demo.leaf", "--direction", "upstream"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "查询方向: upstream" in result.stdout and "<- n_" in result.stdout


def test_mcp_upstream_pages_keep_project_and_scan_filters(store):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (store.root / "test_demo.py").write_text(
        "from demo import leaf\ndef test_leaf(): leaf()\n"
    )

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "callmap", "--cache-dir", str(store.root / "caches"), "mcp"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                args = {
                    "symbol": "demo.leaf",
                    "depth": -1,
                    "budget": 4000,
                    "project_path": str(store.root),
                    "exclude_tests": True,
                    "excludes": [],
                    "scan_mode": "filesystem",
                }
                edges = set()
                pages = 0
                while True:
                    result = await session.call_tool("find_callers", args)
                    assert not result.isError, result.content
                    assert len(result.content) == 1
                    assert len(result.content[0].text) <= args["budget"]
                    graph = json.loads(result.content[0].text)
                    assert graph["direction"] == "upstream"
                    assert not any(n["is_test"] for n in graph["nodes"])
                    edges.update(e["id"] for e in graph["edges"])
                    pages += 1
                    continuation = graph["continuation"]
                    if continuation is None:
                        break
                    assert continuation["tool"] == "find_callers"
                    args = continuation["arguments"]
                    assert args["project_path"] == str(store.root)
                    assert args["exclude_tests"] and args["excludes"] == []
                    assert args["scan_mode"] == "filesystem"
                assert pages > 1 and len(edges) == 8

    asyncio.run(exercise())
