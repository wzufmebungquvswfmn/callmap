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
        "def tail(): pass\ndef neighbor(): tail()\ndef unrelated(): pass\n"
        "def focus_entry():\n    neighbor()\n    unrelated()\n"
        "def query(callback): callback()\n"
        "def read_leaf(): pass\ndef save_leaf(): pass\n"
        "def reading(): query(read_leaf)\ndef saving(): query(save_leaf)\n"
        "async def fetch(*, files=False, readiness_only=False): pass\n"
        "async def flags(enabled):\n"
        "    if enabled: await fetch(files=True)\n"
        "    else: await fetch(readiness_only=True)\n"
        "def report_leaf(): pass\ndef report_middle(): report_leaf()\n"
        "def report_entry(): report_middle()\n"
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
            page.locator("#results .result").click()
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
            page.goto(f"http://127.0.0.1:{server.server_port}/#upstream:demo.leaf")
            expect(page.locator("#direction")).to_have_value("upstream")
            expect(page.locator("#graph .node")).to_have_count(3)
            expect(page.locator("#details")).to_contain_text("当前图中的上游调用点")
            expect(page.locator("#details")).to_contain_text("← demo.entry")
            page.locator("#collapse").click()
            expect(page.locator("#graph .node")).to_have_count(1)
            page.locator("#collapse").click()
            expect(page.locator("#graph .node")).to_have_count(3)
            page.reload()
            expect(page.locator("#direction")).to_have_value("upstream")
            expect(page.locator("#graph .node")).to_have_count(3)
            page.locator("#direction").select_option("downstream")
            expect(page.locator("#graph .node")).to_have_count(1)
            expect(page.locator("#details")).to_contain_text("无已显示的下游调用")
            page.goto(f"http://127.0.0.1:{server.server_port}/#demo.focus_entry")
            expect(page.locator("#graph .node")).to_have_count(4)
            page.locator('.node[aria-label="demo.neighbor"]').click()
            expect(page.locator("#graph .node.selected")).to_have_attribute(
                "aria-label", "demo.neighbor"
            )
            expect(page.locator('[data-layer="foreground"] .node')).to_have_count(3)
            expect(page.locator('[data-layer="foreground"] .edge')).to_have_count(2)
            dimmed = page.locator('.node[aria-label="demo.unrelated"]')
            expect(dimmed).to_have_class("node internal dimmed")
            expect(dimmed).to_have_css("opacity", "0.2")
            expect(page.locator('[data-layer="background"] .edge')).to_have_css(
                "opacity", "0.15"
            )
            assert page.locator("#graph").evaluate(
                "svg => svg.querySelector('[data-layer=background]')"
                ".nextElementSibling === svg.querySelector('[data-layer=foreground]')"
            )
            # Dimmed nodes remain selectable and refresh the focused neighborhood.
            dimmed.click()
            expect(page.locator("#graph .node.selected")).to_have_attribute(
                "aria-label", "demo.unrelated"
            )
            expect(page.locator('[data-layer="foreground"] .node')).to_have_count(2)
            page.goto(f"http://127.0.0.1:{server.server_port}/#upstream:demo.tail")
            expect(page.locator("#graph .node")).to_have_count(3)
            expect(page.locator('[data-layer="foreground"] .node')).to_have_count(2)
            expect(page.locator('[data-layer="background"] .node')).to_have_attribute(
                "aria-label", "demo.focus_entry"
            )
            page.locator('.node[aria-label="demo.neighbor"]').click()
            expect(page.locator('[data-layer="foreground"] .node')).to_have_count(3)
            expect(page.locator('[data-layer="background"] .node')).to_have_count(0)
            page.goto(f"http://127.0.0.1:{server.server_port}/#demo.reading")
            expect(page.locator("#graph .node")).to_have_count(3)
            page.locator('.node[aria-label="demo.query"]').click()
            expect(
                page.locator("#details [data-callback-candidates] summary")
            ).to_contain_text("其他调用点的回调候选（1）")
            page.locator("#details [data-callback-candidates] summary").click()
            expect(page.locator("#details [data-callback-candidates]")).to_contain_text(
                "demo.saving"
            )
            expect(page.locator("#details [data-callback-candidates]")).to_contain_text(
                "demo.save_leaf"
            )
            page.locator("#callbackCandidates").click()
            expect(page.locator("#graph .node")).to_have_count(3)
            expect(page.locator('.node[aria-label="demo.save_leaf"]')).to_be_visible()
            page.goto(f"http://127.0.0.1:{server.server_port}/#demo.flags")
            expect(page.locator("#details .call-facts")).to_have_count(2)
            expect(page.locator("#details .call-facts").first).to_contain_text(
                "调用后等待结果"
            )
            expect(page.locator("#details .call-facts").first).to_contain_text(
                "files=True"
            )
            expect(page.locator("#details .call-facts").last).to_contain_text(
                "readiness_only=True"
            )
            expect(page.locator("#details .call-facts").last).to_contain_text(
                "enabled 为假"
            )
            page.locator("#details .call-facts details").first.locator(
                "summary"
            ).click()
            expect(page.locator("#details .call-facts").first).to_contain_text(
                "不保证运行时调用"
            )
            page.locator("#depth").select_option("1")
            page.goto(f"http://127.0.0.1:{server.server_port}/#demo.report_entry")
            expect(page.locator("#graph .node")).to_have_count(2)
            page.locator("#details [data-recovery-panel] summary").click()
            expect(page.locator("#details [data-recovery-panel]")).to_contain_text(
                "demo.report_middle"
            )
            page.locator("#details [data-recovery]").click()
            expect(page.locator('.node[aria-label="demo.report_leaf"]')).to_be_visible()
            page.locator('[data-relation="callers"]').click()
            expect(page.locator("#direction")).to_have_value("upstream")
            expect(
                page.locator('.node[aria-label="demo.report_entry"]')
            ).to_be_visible()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("mode", ["single", "multiple", "fallback", "empty", "hash"])
def test_browser_discovers_entries_and_only_auto_opens_single_explicit(tmp_path, mode):
    pytest.importorskip("playwright")
    from playwright.sync_api import expect, sync_playwright

    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("Browser smoke test requires local Chrome on macOS")
    (tmp_path / "main.py").write_text(
        "value = 1\n"
        if mode == "empty"
        else "def leaf(): pass\ndef main(): leaf()\ndef other(): pass\n"
    )
    if mode in {"single", "multiple", "hash"}:
        (tmp_path / "pyproject.toml").write_text(
            '[project.scripts]\napp = "main:main"\n'
            + ('other = "main:other"\n' if mode == "multiple" else "")
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
            fragment = "#main.other" if mode == "hash" else ""
            page.goto(f"http://127.0.0.1:{server.server_port}/{fragment}")
            expect(page.locator("#projectRoot")).to_have_text(str(tmp_path))
            if mode in {"single", "hash"}:
                expect(page.locator("#entrySummary")).to_contain_text("1 个明确入口")
                selected = "main.other" if mode == "hash" else "main.main"
                expect(page.locator(".node.selected")).to_have_attribute(
                    "aria-label", selected
                )
                expect(page.locator("#graph .node")).to_have_count(
                    1 if mode == "hash" else 2
                )
                if mode == "single":
                    # Refreshing discovered entries must retain the user's selection.
                    page.locator("#entries .result").filter(
                        has_text="main.other"
                    ).click()
                    expect(page.locator(".node.selected")).to_have_attribute(
                        "aria-label", "main.other"
                    )
                    (tmp_path / "pyproject.toml").write_text(
                        '[project.scripts]\napp = "main:main"\n'
                    )
                    page.locator("#refresh").click()
                    expect(page.locator("#refresh")).to_be_enabled()
                    expect(page.locator(".node.selected")).to_have_attribute(
                        "aria-label", "main.other"
                    )
            elif mode == "multiple":
                expect(page.locator("#entrySummary")).to_contain_text("2 个明确入口")
                expect(page.locator("#graph .node")).to_have_count(0)
                page.locator("#entries .result").filter(has_text="main.main").click()
                expect(page.locator("#graph .node")).to_have_count(2)
            elif mode == "fallback":
                expect(page.locator("#entrySummary")).to_contain_text("0 个明确入口")
                expect(page.locator("#entries .result")).to_have_count(2)
                expect(page.locator("#notice")).to_contain_text("候选入口不会自动展开")
                expect(page.locator("#graph .node")).to_have_count(0)
            else:
                expect(page.locator("#entries")).to_contain_text("未识别到入口")
                expect(page.locator("#graph .node")).to_have_count(0)
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


def test_browser_entry_response_does_not_override_manual_selection(tmp_path):
    pytest.importorskip("playwright")
    from playwright.sync_api import expect, sync_playwright

    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.exists():
        pytest.skip("Browser smoke test requires local Chrome on macOS")
    (tmp_path / "main.py").write_text("def main(): pass\ndef other(): pass\n")
    (tmp_path / "pyproject.toml").write_text('[project.scripts]\napp = "main:main"\n')
    server = make_server(GraphStore(tmp_path), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(chrome))
            page = browser.new_page()
            pending = []
            page.route("**/api/entries?*", lambda route: pending.append(route))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.locator("#query").fill("main.other")
            page.locator("#searchForm button").click()
            page.locator("#results .result").click()
            expect(page.locator(".node.selected")).to_have_attribute(
                "aria-label", "main.other"
            )
            assert pending
            route = pending.pop()
            route.fulfill(response=route.fetch())
            expect(page.locator("#entrySummary")).to_contain_text("1 个明确入口")
            expect(page.locator(".node.selected")).to_have_attribute(
                "aria-label", "main.other"
            )
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
