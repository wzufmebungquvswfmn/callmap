"""Facts, explanations and completeness reflect the actual query scope."""

from callmap.store import GraphStore
from callmap.views import bounded_graph, serialize


def project(tmp_path, source):
    (tmp_path / "main.py").write_text(source)
    return GraphStore(tmp_path)


def root(graph):
    return next(n for n in graph["nodes"] if n["id"] == graph["entry"])


def test_import_resolution_is_not_a_runtime_guarantee(tmp_path):
    store = project(tmp_path, "from target import work\ndef entry(): work()\n")
    (tmp_path / "target.py").write_text("def work(): pass\n")
    edge = store.expand("entry")["edges"][0]
    assert edge["status"] == "resolved"
    assert edge["resolution"]["basis"] == ["direct_import"]
    assert edge["resolution"]["runtime_guaranteed"] is False
    assert "不保证运行时调用" in edge["resolution"]["summary"]
    assert edge["callsite"]["path"] == "main.py"


def test_candidate_count_uses_selected_callsite_context(tmp_path):
    store = project(
        tmp_path,
        """
def read(): pass
def save(): pass
def query(callback): callback()
def reading(): query(read)
def saving(): query(save)
""",
    )
    edge = next(
        e for e in store.expand("reading")["edges"] if e["expression"] == "callback"
    )
    assert edge["resolution"]["candidate_count"] == 1
    assert edge["resolution"]["indexed_candidate_count"] == 2
    assert "当前查询路径" in edge["resolution"]["summary"]


def test_interface_call_reports_three_candidates_and_receiver_conditions(tmp_path):
    store = project(
        tmp_path,
        """
class Base:
    def fetch(self): pass
class Full(Base):
    def fetch(self): pass
class Preview(Base):
    def fetch(self): pass
def entry(service: Base): service.fetch()
""",
    )
    edges = store.expand("entry")["edges"]
    assert len(edges) == 3
    for edge in edges:
        resolution = edge["resolution"]
        assert resolution["candidate_count"] == 3
        assert "3 个静态候选" in resolution["summary"]
        assert any("接收对象" in c for c in resolution["conditions"])
        assert any("接收对象" in u for u in resolution["unknowns"])


def test_business_call_arguments_defaults_branches_and_await(tmp_path):
    store = project(
        tmp_path,
        """
async def fetch_bundle(*, files=False, text=False, readiness_only=False): pass
async def entry(enabled):
    if enabled:
        await fetch_bundle(files=True, text=True)
    else:
        await fetch_bundle(readiness_only=True)
""",
    )
    edges = store.expand("entry")["edges"]
    assert len(edges) == 2
    normal, readiness = sorted(edges, key=lambda e: e["line"])
    values = {a["parameter"]: a["value"] for a in normal["callsite"]["arguments"]}
    assert values == {"files": True, "text": True, "readiness_only": False}
    assert any(a["kind"] == "default" for a in normal["callsite"]["arguments"])
    assert normal["callsite"]["execution"] == {"mode": "await_result", "awaited": True}
    assert normal["callsite"]["conditions"][0]["truthy"] is True
    assert readiness["callsite"]["conditions"][0]["truthy"] is False
    assert readiness["callsite"]["arguments"][0]["value"] is True


def test_constants_unknown_inputs_and_short_circuit_are_distinct(tmp_path):
    store = project(
        tmp_path,
        """
FLAG = True
def work(value): pass
def entry(dynamic):
    dynamic and work(FLAG)
    work(dynamic)
""",
    )
    first, second = sorted(store.expand("entry")["edges"], key=lambda e: e["line"])
    assert first["callsite"]["arguments"][0]["value"] is True
    assert first["callsite"]["conditions"][0]["kind"] == "short_circuit"
    assert second["callsite"]["arguments"][0]["known"] is False
    assert second["callsite"]["conditions"] == []


def test_data_dictionary_update_does_not_claim_callback_registration(tmp_path):
    store = project(
        tmp_path,
        """
CACHE = {}
def save(key, value): CACHE[key] = value
def entry(): save("count", 1)
""",
    )
    assert store.expand("entry")["edges"][0]["callsite"]["execution"]["mode"] == "call"


def test_unknown_forwarded_input_is_reported_when_global_candidates_are_hidden(
    tmp_path,
):
    store = project(
        tmp_path,
        """
def read(): pass
def query(callback): callback()
def outer(callback): query(callback)
def known(): outer(read)
def dynamic(callback): outer(callback)
""",
    )
    graph = store.expand("dynamic")
    assert "main.read" not in {n["qualified_name"] for n in graph["nodes"]}
    assert graph["completeness"]["unknown_callback_scope_count"] == 1
    assert any(r["kind"] == "unresolved_input" for r in graph["recovery"])
    assert graph["completeness"]["unresolved_callsite_count"] == 1


def test_execution_wrapper_keeps_submission_and_waiting_separate(tmp_path):
    store = project(
        tmp_path,
        """
import asyncio
def worker(): pass
async def task(): pass
async def entry():
    await asyncio.to_thread(worker)
    asyncio.create_task(task())
""",
    )
    worker = store.callers("worker")["edges"][0]
    assert worker["callsite"]["execution"]["mode"] == "thread_submission"
    assert worker["callsite"]["execution"]["awaited"] is True
    assert "stdlib_contract" in worker["resolution"]["basis"]
    task = store.callers("task")["edges"][0]
    assert task["callsite"]["execution"]["mode"] == "task_submission"


def test_registry_and_manual_evidence_have_explicit_conditions(tmp_path):
    store = project(
        tmp_path,
        """
REGISTRY = {}
def work(): pass
def register(key, callback): REGISTRY[key] = callback
def setup(): register("work", work)
def factory(key): REGISTRY[key]()
def unknown(callback): callback()
""",
    )
    edge = store.expand("setup")["edges"][0]
    assert edge["callsite"]["execution"]["mode"] == "registry_write_call"
    registry = store.expand("factory")["edges"][0]["resolution"]
    assert "registry" in registry["basis"]
    assert any("注册项已加载" in condition for condition in registry["conditions"])
    unknown = store.expand("unknown")
    target = next(n for n in unknown["nodes"] if n["kind"] == "unresolved")
    store.annotate(target["id"], "configured callback", ["work"])
    manual = store.expand("unknown")["edges"][0]
    assert "manual_annotation" in manual["resolution"]["basis"]
    assert manual["resolution"]["runtime_guaranteed"] is False
    assert any(e["kind"] == "manual-annotation" for e in manual["evidence"])


def test_upstream_root_can_be_complete_while_deeper_callers_are_omitted(tmp_path):
    store = project(
        tmp_path,
        """
def leaf(): pass
def parent(): leaf()
def entry(): parent()
""",
    )
    graph = store.callers("leaf", depth=1)
    assert graph["truncated"]
    assert root(graph)["completeness"]["callers"]["complete"] is True
    recovery = next(r for r in graph["recovery"] if r["kind"] == "unexpanded")
    assert recovery["symbol"] == "main.parent"
    assert recovery["causes"] == ["depth_limit"]
    query = recovery["next_query"]
    assert query["tool"] == "find_callers"
    assert query["arguments"]["project_path"] == str(store.root)
    following = store.callers(query["arguments"]["symbol"], depth=1)
    assert "main.entry" in {n["qualified_name"] for n in following["nodes"]}
    limited = store.callers("leaf", max_nodes=1)
    assert root(limited)["completeness"]["callers"]["complete"] is False
    assert limited["recovery"][0]["causes"] == ["node_limit"]


def test_empty_callers_unresolved_relations_and_parse_errors_do_not_prove_absence(
    tmp_path,
):
    store = project(tmp_path, "def entry(callback): callback()\n")
    (tmp_path / "broken.py").write_text("def broken(:\n")
    graph = store.callers("entry")
    assert root(graph)["completeness"]["callers"]["state"] == "none_identified"
    assert graph["completeness"]["runtime_complete"] is False
    assert graph["completeness"]["parse_error_count"] == 1
    assert graph["completeness"]["unresolved_index_callsite_count"] == 1
    expanded = store.expand("entry", depth=0)
    unresolved = next(r for r in expanded["recovery"] if r["kind"] == "unresolved")
    assert unresolved["next_query"]["tool"] == "read_symbol"
    assert unresolved["callsites"][0]["expression"] == "callback"
    assert "没有识别到关系" in store.context("entry")


def test_pagination_preserves_graph_completeness_and_scoped_recovery(tmp_path):
    source = "def leaf(): pass\n" + "\n".join(
        f"def caller_{i}(): leaf()" for i in range(12)
    )
    store = project(tmp_path, source)
    graph = store.callers("leaf", max_nodes=5)
    query = {"symbol": "leaf", "max_nodes": 5, "budget": 4000}
    page = bounded_graph(graph, query, budget=4000)
    all_edges, recovery = set(), []
    while True:
        assert len(serialize(page)) <= 4000
        assert root(page)["completeness"]["callers"]["response_complete"] is False
        assert page["completeness"]["runtime_complete"] is False
        all_edges.update(e["id"] for e in page["edges"])
        recovery.extend(page["recovery"])
        if not page["continuation"]:
            break
        next_page = page["continuation"]["arguments"]
        page = bounded_graph(
            graph,
            query,
            budget=4000,
            cursor=next_page["cursor"],
            snapshot=next_page["snapshot"],
        )
    assert all_edges == {e["id"] for e in graph["edges"]}
    assert recovery == graph["recovery"]
    assert recovery[0]["next_query"]["arguments"]["excludes"] == store.excludes
