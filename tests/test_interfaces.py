import asyncio
import json
import sys
import threading
import urllib.error
import urllib.request

import pytest

from callmap.server import make_server
from callmap.store import GraphStore


@pytest.fixture
def store(tmp_path):
    (tmp_path / "demo.py").write_text(
        "def leaf(): return 1\ndef entry(): return leaf()\n"
        "handlers = {'one': leaf, 'two': entry}\n"
        "def factory(key): handlers[key]()\n"
    )
    return GraphStore(tmp_path)


def test_http_search_graph_node_and_write_token(store):
    server = make_server(store, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/search?q=entry") as response:
            result = json.load(response)
        assert result[0]["qualified_name"] == "demo.entry"
        with urllib.request.urlopen(base + "/api/graph?symbol=demo.entry") as response:
            result = json.load(response)
        assert len(result["nodes"]) == 2
        with urllib.request.urlopen(base + "/api/node?symbol=demo.leaf") as response:
            assert "return 1" in json.load(response)["code"]
        request = urllib.request.Request(
            base + "/api/annotate", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_mcp_stdio_roundtrip(store):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "callmap", "--project", str(store.root), "mcp"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                assert {"expand_calls", "read_symbol", "get_context"} <= {
                    t.name for t in result.tools
                }
                result = await session.call_tool(
                    "expand_calls", {"symbol": "demo.entry"}
                )
                assert not result.isError
                graph = json.loads(result.content[0].text)
                assert len(graph["nodes"]) == 2
                result = await session.call_tool("read_symbol", {"symbol": "demo.leaf"})
                assert "return 1" in result.content[0].text
                result = await session.call_tool(
                    "expand_calls", {"symbol": "demo.factory", "depth": 1}
                )
                graph = json.loads(result.content[0].text)
                candidates = [
                    e for e in graph["edges"] if e["source"] == graph["entry"]
                ]
                assert len(candidates) == 2
                assert all(
                    e["status"] == "candidate" and e["evidence"] for e in candidates
                )

    asyncio.run(exercise())
