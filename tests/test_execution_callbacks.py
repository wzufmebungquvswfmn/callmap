"""Execution contracts and callback paths retain their callsite evidence."""

from callmap.store import GraphStore
from callmap.views import bounded_graph


def project(tmp_path, source):
    (tmp_path / "main.py").write_text(source)
    return GraphStore(tmp_path)


def names(graph):
    return {n["qualified_name"] for n in graph["nodes"]}


def test_stdlib_execution_wrappers_are_visible_to_callers(tmp_path):
    store = project(
        tmp_path,
        """
import asyncio as aio
from contextvars import copy_context
class Client:
    def send(self): pass
def worker(client): client.send()
async def entry():
    await aio.to_thread(worker, Client())
    aio.get_running_loop().run_in_executor(None, worker, Client())
    copy_context().run(worker, Client())
""",
    )
    graph = store.callers("worker")
    edges = [e for e in graph["edges"] if e["reason"] == "execution-callback"]
    assert len(edges) == 3
    assert {e["evidence"][0]["execution"] for e in edges} == {
        "thread",
        "executor",
        "context",
    }
    assert all(e["synthetic"] and e["status"] == "candidate" for e in edges)
    assert all(e["evidence"][0]["argument"] == "worker" for e in edges)
    assert "main.Client.send" in names(store.expand("entry"))
    assert "main.worker" in names(store.expand("entry", context_entry="entry"))


def test_wrapper_callback_selection_and_expandable_alternatives(tmp_path):
    store = project(
        tmp_path,
        """
def read(): pass
def save(): pass
class Diagnostics:
    def query(self, callback):
        selected = callback
        return selected()
diagnostics = Diagnostics()
def reading(): return diagnostics.query(read)
def saving(): return diagnostics.query(callback=save)
def both():
    reading()
    saving()
""",
    )
    graph = store.expand("reading")
    assert "main.read" in names(graph)
    assert "main.save" not in names(graph)
    wrapper = next(n for n in graph["nodes"] if n["name"] == "query")
    assert wrapper["callback_candidates"][0]["target"] == "main.save"
    assert wrapper["callback_candidates"][0]["evidence"][0]["caller"] == ("main.saving")
    page = bounded_graph(graph, {"symbol": "reading"})
    compact = next(n for n in page["nodes"] if n["id"] == wrapper["id"])
    assert compact["callback_candidate_count"] == 1
    assert compact["callback_candidates_query"]["symbol"] == wrapper["id"]
    assert {"main.read", "main.save"} <= names(store.expand(wrapper["id"]))
    assert {"main.read", "main.save"} <= names(store.expand("both"))
    assert "main.read" not in names(store.expand("saving"))
    assert {"main.reading", "main.saving"} <= names(store.callers("Diagnostics.query"))


def test_unknown_callback_input_remains_explicitly_uncertain(tmp_path):
    store = project(
        tmp_path,
        """
import asyncio
def known(): pass
def query(callback): callback()
def known_entry(): query(known)
def dynamic_entry(callback):
    query(callback)
    asyncio.to_thread(callback)
""",
    )
    graph = store.expand("dynamic_entry")
    assert "main.known" not in names(graph)
    assert any(
        e["expression"] == "callback" and e["status"] == "unresolved"
        for e in graph["edges"]
    )
    edge = next(e for e in graph["edges"] if e["reason"] == "execution-callback")
    assert edge["status"] == "unresolved"
    assert edge["evidence"][0]["argument"] == "callback"


def test_unrelated_wrapper_names_do_not_create_execution_contracts(tmp_path):
    store = project(
        tmp_path,
        """
def worker(): pass
class Custom:
    def to_thread(self, fn): pass
def entry(): Custom().to_thread(worker)
""",
    )
    assert "main.worker" not in names(store.expand("entry"))


def test_task_submission_keeps_existing_coroutine_call(tmp_path):
    store = project(
        tmp_path,
        """
import asyncio
async def worker(): pass
def entry(): asyncio.create_task(worker())
""",
    )
    edges = store.callers("worker")["edges"]
    assert len(edges) == 1
    assert edges[0]["expression"] == "worker"
    assert edges[0]["evidence"][0]["kind"] == "coroutine-submission"
    assert edges[0]["evidence"][0]["api"] == "asyncio.create_task"


def test_forwarded_callback_context_survives_multiple_wrappers(tmp_path):
    store = project(
        tmp_path,
        """
import asyncio
def read(): pass
def save(): pass
def query(callback): callback()
def dispatch(callback): asyncio.to_thread(callback)
def outer(callback):
    selected = callback
    query(selected)
    dispatch(selected)
def reading(): outer(read)
def saving(): outer(save)
""",
    )
    graph = store.expand("reading")
    assert "main.read" in names(graph)
    assert "main.save" not in names(graph)
    graph = store.expand("saving")
    assert "main.save" in names(graph)
    assert "main.read" not in names(graph)


def test_default_callback_and_distinct_calls_on_one_line(tmp_path):
    store = project(
        tmp_path,
        """
def read(): pass
def save(): pass
def query(callback=read): callback()
def reading(): query()
def both(): query(read); query(callback=save)
""",
    )
    assert "main.save" not in names(store.expand("reading"))
    graph = store.expand("both")
    assert {"main.read", "main.save"} <= names(graph)
    incoming = [e for e in graph["edges"] if e["expression"] == "query"]
    assert len(incoming) == 2
    assert incoming[0]["line"] == incoming[1]["line"]
    assert incoming[0]["column"] != incoming[1]["column"]


def test_forwarded_callable_instances_remain_separate(tmp_path):
    store = project(
        tmp_path,
        """
class Read:
    def __call__(self): pass
class Save:
    def __call__(self): pass
def query(callback): callback()
def reading(): query(Read())
def saving(): query(Save())
""",
    )
    assert "main.Read.__call__" in names(store.expand("reading"))
    assert "main.Save.__call__" not in names(store.expand("reading"))


def test_external_instance_callback_does_not_require_project_source(tmp_path):
    store = project(
        tmp_path,
        """
def work(): pass
def query(callback): callback()
def known(): query(work)
def external(): query(1)
""",
    )
    graph = store.expand("external")
    assert "main.work" not in names(graph)
    assert any(e["status"] == "unresolved" for e in graph["edges"])
