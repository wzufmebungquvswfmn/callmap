import asyncio
import json
import sys

import pytest

from callmap.projects import ProjectStores
from callmap.store import GraphStore


def test_paths_and_same_name_project_isolation(tmp_path):
    first = tmp_path / "one" / "repo"
    second = tmp_path / "two" / "repo"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "demo.py").write_text("def entry(): return 1\n")
    (second / "demo.py").write_text("def entry(): return 2\n")
    projects = ProjectStores(cache_root=tmp_path / "cache")
    assert not projects.cache_root.exists()
    for path in (None, "", ".", "relative/path", str(tmp_path / "missing")):
        with pytest.raises(ValueError):
            projects.get(path)
    assert not projects.cache_root.exists()
    a = projects.get(str(first))
    b = projects.get(str(second))
    assert a.cache != b.cache
    assert projects.get(str(first / ".." / "repo")) is a
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    assert projects.get(str(alias)) is a
    a.annotate("demo.entry", "first project only")
    assert a.select(a.graph(), "demo.entry")["doc"] == "first project only"
    assert b.select(b.graph(), "demo.entry")["doc"] == ""
    assert not (first / ".callmap").exists()
    assert not (second / ".callmap").exists()
    restarted = ProjectStores(cache_root=projects.cache_root)
    assert restarted.get(str(first)).stats()["cache_hit"]


def test_explicit_default_remains_compatible(tmp_path):
    (tmp_path / "demo.py").write_text("def entry(): return 1\n")
    default = GraphStore(tmp_path, tmp_path / "legacy-cache")
    projects = ProjectStores(default, tmp_path / "global-cache")
    assert projects.get() is default
    assert projects.get(str(tmp_path)) is default
    assert not (tmp_path / "global-cache").exists()


def test_global_mcp_multiple_projects_from_unrelated_cwd(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "demo.py").write_text(
        "def leaf(): return 'first'\ndef entry(): return leaf()\n"
    )
    (second / "demo.py").write_text(
        "def other(): return 'second'\ndef entry(): return other()\n"
    )
    cache = tmp_path / "cache"

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "callmap", "--cache-dir", str(cache), "mcp"],
            cwd=str(tmp_path),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert "project_path" in initialized.instructions
                tools = (await session.list_tools()).tools
                assert len(tools) == 8
                assert "find_entry_points" in {tool.name for tool in tools}
                assert "find_callers" in {tool.name for tool in tools}
                assert all("project_path" in t.inputSchema["properties"] for t in tools)
                assert not cache.exists()
                missing = await session.call_tool("index_status", {})
                assert missing.isError and "project_path" in missing.content[0].text
                relative = await session.call_tool(
                    "index_status", {"project_path": "."}
                )
                assert relative.isError
                assert not cache.exists()

                async def call(tool, project, **args):
                    result = await session.call_tool(
                        tool, {"project_path": str(project), **args}
                    )
                    assert not result.isError, result.content
                    return result.content[0].text

                stats = [
                    json.loads(await call("index_status", p)) for p in (first, second)
                ]
                assert stats[0]["cache_dir"] != stats[1]["cache_dir"]
                for project, leaf in (
                    (first, "demo.leaf"),
                    (second, "demo.other"),
                    (first, "demo.leaf"),
                ):
                    graph = json.loads(
                        await call("expand_calls", project, symbol="demo.entry")
                    )
                    assert {n["qualified_name"] for n in graph["nodes"]} == {
                        "demo.entry",
                        leaf,
                    }
                    upstream = json.loads(
                        await call("find_callers", project, symbol=leaf)
                    )
                    assert upstream["direction"] == "upstream"
                    assert {n["qualified_name"] for n in upstream["nodes"]} == {
                        "demo.entry",
                        leaf,
                    }
                    search = json.loads(
                        await call("search_functions", project, query="entry")
                    )
                    if isinstance(search, dict):
                        search = search.get("result", [search])
                    assert search[0]["qualified_name"] == "demo.entry"
                    node = json.loads(
                        await call("read_symbol", project, symbol="demo.entry")
                    )
                    assert leaf.split(".")[1] + "()" in node["code"]
                await call(
                    "annotate_symbol", first, symbol="demo.entry", doc="only first"
                )
                other = json.loads(
                    await call("read_symbol", second, symbol="demo.entry")
                )
                assert other["doc"] == ""
                context = await call(
                    "get_context", first, symbol="demo.entry", include_code=True
                )
                assert "only first" in context and "```python" in context
                (first / "demo.py").write_text("def entry(): return 99\n")
                refreshed = json.loads(
                    await call("read_symbol", first, symbol="demo.entry")
                )
                assert "return 99" in refreshed["code"]
                assert not (tmp_path / ".callmap").exists()

    asyncio.run(exercise())
