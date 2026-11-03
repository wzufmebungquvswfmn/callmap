import threading
from pathlib import Path

import pytest

from callmap.server import make_server
from callmap.store import GraphStore


def test_browser_search_expand_collapse_annotation_and_export(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import expect, sync_playwright

    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("Browser smoke test requires local Chrome on macOS")
    (tmp_path / "demo.py").write_text(
        "def leaf(): return 1\ndef entry(n):\n    leaf()\n    if n: entry(n - 1)\n"
        "def other(): return 2\nhandlers = {'one': leaf, 'two': other}\n"
        "def factory(key): handlers[key]()\n"
    )
    store = GraphStore(tmp_path)
    server = make_server(store, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(chrome))
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.locator("#query").fill("demo.entry")
            page.locator("#searchForm button").click()
            page.locator(".result").click()
            expect(page.locator("#graph .node")).to_have_count(2)
            expect(page.locator("#details pre").last).to_contain_text("if n: entry")
            page.locator("#collapse").click()
            expect(page.locator("#graph .node")).to_have_count(1)
            page.locator("#collapse").click()
            expect(page.locator("#graph .node")).to_have_count(2)
            before = float(page.locator("#graph").get_attribute("width"))
            page.locator("#zoomOut").click()
            assert float(page.locator("#graph").get_attribute("width")) < before
            page.locator("#manualDoc").fill("入口说明 <script>alert(1)</script>")
            page.locator("#save").click()
            expect(page.locator("#saveStatus")).to_contain_text("已保存")
            page.reload()
            expect(page.locator(".doc")).to_contain_text("<script>alert(1)</script>")
            with page.expect_download() as download:
                page.locator("#download").click()
            assert download.value.suggested_filename == "callmap-graph.json"
            page.goto(f"http://127.0.0.1:{server.server_port}/#demo.factory")
            expect(page.locator("#graph .node")).to_have_count(3)
            expect(page.locator(".edge-detail .meta")).to_have_count(2)
            expect(page.locator(".edge-detail .meta").first).to_contain_text(
                "registry-write"
            )
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
