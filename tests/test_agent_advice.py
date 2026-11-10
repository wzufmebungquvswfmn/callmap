"""Agent workflows: evidence, guards, query scopes, search and Git impact."""

import asyncio
import json
import subprocess
import sys

import pytest

from callmap.contexts import AnalysisContexts
from callmap.impact import impact
from callmap.projects import ProjectStores
from callmap.store import GraphStore
from callmap.views import bounded_graph

FACTORY = """
import os
class Settings:
    transport = "http"
    def load(self): self.transport = os.getenv("TRANSPORT", "http")
class RpcClient:
    def send(self): pass
class HttpClient:
    def send(self): pass
def factory(settings):
    if settings.transport == "rpc":
        return RpcClient()
    return HttpClient()
def submit(settings: Settings):
    client = factory(settings)
    client.send()
"""


def make_store(tmp_path, source=FACTORY):
    (tmp_path / "main.py").write_text(source)
    return GraphStore(tmp_path)


def test_factory_conditions_reach_method_call_with_config_definitions(tmp_path):
    store = make_store(tmp_path)
    graph = store.expand("submit")
    nodes = {n["id"]: n for n in graph["nodes"]}
    sends = {
        nodes[e["target"]]["qualified_name"]: e
        for e in graph["edges"]
        if e["expression"] == "client.send"
    }
    assert set(sends) == {"main.RpcClient.send", "main.HttpClient.send"}
    for target, truthy in (
        ("main.RpcClient.send", True),
        ("main.HttpClient.send", False),
    ):
        edge = sends[target]
        conditions = edge["callsite"]["guard_paths"][0]["conditions"]
        assert conditions[0]["expression"] == "settings.transport == 'rpc'"
        assert conditions[0]["truthy"] is truthy
        assert edge["resolution"]["configuration_sources"][0]["known"] is False
        assert any(
            "os.getenv" in r["initializer"]
            for r in edge["resolution"]["configuration_sources"][0]["sources"]
        )
        assert "factory_return" in edge["explanation"]["basis"]
    assert any(
        r.get("gap_kind") == "configuration_value_unknown" for r in graph["recovery"]
    )
    page = bounded_graph(graph, {}, budget=20000)
    assert all(e.get("explanation") for e in page["edges"])


def test_nested_factory_aliases_and_argument_names_are_propagated(tmp_path):
    source = FACTORY.replace("client = factory(settings)", "client = forward(settings)")
    source += "\ndef forward(cfg):\n    selected = factory(cfg)\n    return selected\n"
    graph = make_store(tmp_path, source).expand("submit")
    sends = [e for e in graph["edges"] if e["expression"] == "client.send"]
    assert len(sends) == 2
    assert all(
        "settings.transport"
        in e["callsite"]["guard_paths"][0]["conditions"][0]["expression"]
        for e in sends
    )


def test_structured_search_and_reference_definition_override_read(tmp_path):
    store = make_store(tmp_path)
    assert len(store.search(owner="RpcClient")) == 2
    assert store.search("RpcClient", symbol_type="class")[0]["symbol_type"] == "class"
    assert not store.search("RpcClient", path="other/**")
    definitions = store.references("transport", kind="definition")["result"]
    assert any(r["initializer"] == "'http'" for r in definitions)
    writes = store.references("transport", kind="write")["result"]
    assert any("os.getenv" in r["initializer"] for r in writes)
    assert store.references("transport", kind="read")["result"]
    page = store.references("transport", limit=1)
    assert page["next_offset"] == 1


def test_framework_route_search_is_not_deployment_proof(tmp_path):
    store = make_store(
        tmp_path,
        """
from fastapi import FastAPI
app = FastAPI()
@app.post("/submit")
def handler(): pass
def unused(): pass
""",
    )
    result = store.search(route="/submit")
    assert result[0]["qualified_name"] == "main.handler"
    assert result[0]["entry_kinds"] == ["framework"]
    assert store.search(entry_kind="public_root")
    assert all(e["deployment_verified"] is False for e in store.entries()["entries"])


def test_context_reuse_and_expiry_for_source_and_annotations(tmp_path):
    store = make_store(tmp_path, "def entry(): pass\n")
    contexts = AnalysisContexts(ProjectStores(store))
    selected, entry, spec, expected = contexts.resolve()
    token = contexts.bind(selected, spec, selected._query_graph(entry), expected)
    selected, entry, spec, expected = contexts.resolve(analysis_context=token)
    assert contexts.bind(selected, spec, selected.read_node("entry"), expected) == token
    with pytest.raises(ValueError, match="conflicts"):
        contexts.resolve(analysis_context=token, scan_mode="filesystem")
    store.annotate("entry", "new evidence")
    with pytest.raises(ValueError, match="expired"):
        contexts.bind(selected, spec, selected.read_node("entry"), expected)
    with pytest.raises(ValueError, match="expired"):
        store.annotate(
            "entry",
            "stale write",
            expected_fingerprint=expected["analysis_fingerprint"],
        )
    (tmp_path / "main.py").write_text("def entry(): return 1\n")
    with pytest.raises(ValueError, match="expired"):
        contexts.bind(selected, spec, selected.read_node("entry"), expected)


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )
    return result.stdout


def git_store(tmp_path):
    store = make_store(tmp_path, "def leaf(): return 1\ndef entry(): return leaf()\n")
    (tmp_path / "test_main.py").write_text(
        "from main import leaf\ndef test_leaf(): assert leaf() == 1\n"
    )
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "main.py", "test_main.py")
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-qm",
        "base",
    )
    return store


def test_git_impact_links_modified_removed_and_renamed_symbols(tmp_path):
    store = git_store(tmp_path)
    (tmp_path / "main.py").write_text(
        "def leaf(): return 2\ndef entry(): return leaf()\n"
    )
    result = impact(store)
    leaf = next(r for r in result["result"] if r["symbol"] == "main.leaf")
    assert any(e["symbol"] == "main.entry" for e in leaf["after"]["impact"]["entries"])
    assert any(
        t["symbol"] == "test_main.test_leaf" for t in leaf["after"]["impact"]["tests"]
    )
    assert leaf["after"]["impact"]["test_coverage_verified"] is False
    (tmp_path / "main.py").write_text("def entry(): return 2\n")
    removed = next(r for r in impact(store)["result"] if r["symbol"] == "main.leaf")
    assert removed["change"] == "removed"
    assert removed["before"]["impact"]["tests"]


def test_impact_scopes_excluded_tests_and_validates_revision(tmp_path):
    store = git_store(tmp_path)
    (tmp_path / "main.py").write_text(
        "def leaf(): return 2\ndef entry(): return leaf()\n"
    )
    filtered = GraphStore(tmp_path, excludes=["test_*.py"])
    result = impact(filtered)
    assert result["excluded_tests"] is True
    assert all(
        not r["after"]["impact"]["tests"] for r in result["result"] if r["after"]
    )
    with pytest.raises(ValueError):
        impact(store, base="--output=bad")


def test_git_file_rename_and_supplied_patch_keep_baseline_links(tmp_path):
    store = git_store(tmp_path)
    (tmp_path / "main.py").rename(tmp_path / "renamed.py")
    git(tmp_path, "add", "-A")
    patch = git(
        tmp_path,
        "-c",
        "core.quotepath=false",
        "diff",
        "--cached",
        "--find-renames",
        "HEAD",
    )
    result = impact(store, patch=patch)
    renamed = next(r for r in result["result"] if r["symbol"] == "renamed.leaf")
    assert renamed["change"] == "renamed_file"
    assert renamed["before"]["impact"]["tests"]
    assert renamed["after"]["id"] != renamed["before"]["id"]


def test_context_git_commit_change_requires_new_scope(tmp_path):
    store = git_store(tmp_path)
    contexts = AnalysisContexts(ProjectStores(store))
    selected, entry, spec, expected = contexts.resolve()
    token = contexts.bind(selected, spec, selected._query_graph(entry), expected)
    _, _, spec, expected = contexts.resolve(analysis_context=token)
    git(
        tmp_path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "new commit",
    )
    with pytest.raises(ValueError, match="expired"):
        contexts.bind(selected, spec, selected._query_graph(entry), expected)


def test_mcp_context_only_queries_and_annotation_rotation(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    make_store(tmp_path, "def leaf(): pass\ndef entry(): leaf()\n")

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "callmap", "--project", str(tmp_path), "mcp"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **args):
                    result = await session.call_tool(name, args)
                    assert not result.isError, result.content
                    return json.loads(result.content[0].text)

                opened = await call("open_analysis_context")
                token = opened["analysis_context"]
                result = await call(
                    "search_functions", query="entry", analysis_context=token
                )
                assert result["result"][0]["qualified_name"] == "main.entry"
                graph = await call(
                    "expand_calls", symbol="entry", analysis_context=token
                )
                assert graph["analysis_context"] == token
                assert await call("read_symbol", symbol="entry", analysis_context=token)
                changed = await call(
                    "annotate_symbol",
                    symbol="entry",
                    doc="evidence",
                    analysis_context=token,
                )
                assert changed["analysis_context"] != token
                invalid = await session.call_tool(
                    "read_symbol", {"symbol": "entry", "analysis_context": token}
                )
                assert invalid.isError

    asyncio.run(exercise())
