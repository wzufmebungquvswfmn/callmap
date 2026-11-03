from callmap.store import GraphStore


def make_store(tmp_path, source, library=None):
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(source)
    libraries = []
    if library:
        folder = tmp_path / "lib"
        folder.mkdir()
        (folder / "sdk.py").write_text(library)
        libraries = [str(folder)]
    return GraphStore(project, library_roots=libraries)


def direct(store, symbol):
    graph = store.graph()
    source = store.select(graph, symbol)["id"]
    nodes = {n["id"]: n for n in graph["nodes"]}
    return [(nodes[e["target"]], e) for e in graph["edges"] if e["source"] == source]


def test_external_factory_annotation_and_chain_without_import(tmp_path):
    store = make_store(
        tmp_path,
        """
from sdk import make
def entry():
    make().send().close()
""",
        '''
raise RuntimeError("The analyzer must never import this SDK")
class Reply:
    def close(self):
        """Close the reply."""
class Client:
    def send(self) -> "Reply": pass
def make() -> Client: pass
''',
    )
    edges = direct(store, "entry")
    assert {n["qualified_name"] for n, _ in edges} == {
        "sdk.make",
        "sdk.Client.send",
        "sdk.Reply.close",
    }
    assert next(e for n, e in edges if n["name"] == "close")["status"] == "candidate"


def test_stdlib_factory_and_native_class_methods(tmp_path):
    store = make_store(
        tmp_path,
        """
import logging
import contextvars
log = logging.getLogger(__name__)
ctx = contextvars.ContextVar("ctx")
def entry():
    log.info("hello")
    ctx.get()
""",
    )
    assert {n["qualified_name"] for n, _ in direct(store, "entry")} == {
        "logging.Logger.info",
        "contextvars.ContextVar.get",
    }


def test_parameter_propagation_multiple_hops_and_keyword_arguments(tmp_path):
    store = make_store(
        tmp_path,
        """
class A:
    def send(self): pass
class B:
    def send(self): pass
class Unrelated:
    def send(self): pass
def inner(client): client.send()
def middle(value): inner(client=value)
def entry():
    middle(A())
    middle(B())
""",
    )
    edges = direct(store, "inner")
    assert {n["qualified_name"] for n, _ in edges} == {"main.A.send", "main.B.send"}
    assert all(e["status"] == "candidate" for _, e in edges)


def test_callback_return_and_constructor_argument_fields(tmp_path):
    store = make_store(
        tmp_path,
        """
def done(): pass
def choose(callback): return callback
class Client:
    def send(self): pass
class Service:
    def __init__(self, client): self.client = client
    def run(self): self.client.send()
def entry():
    callback = choose(done)
    callback()
    Service(Client()).run()
""",
    )
    assert any(n["qualified_name"] == "main.done" for n, _ in direct(store, "entry"))
    assert {n["qualified_name"] for n, _ in direct(store, "Service.run")} == {
        "main.Client.send"
    }


def test_bound_and_unbound_methods_do_not_shift_arguments(tmp_path):
    store = make_store(
        tmp_path,
        """
class A:
    def send(self): pass
class Service:
    def run(self, client): client.send()
    @staticmethod
    def static(client): client.send()
    @classmethod
    def class_method(cls, client): client.send()
def entry():
    s = Service()
    s.run(A())
    Service.run(s, A())
    Service.static(A())
    Service.class_method(A())
""",
    )
    for symbol in ("Service.run", "Service.static", "Service.class_method"):
        assert {n["qualified_name"] for n, _ in direct(store, symbol)} == {
            "main.A.send"
        }


def test_unknown_receiver_and_unknown_star_do_not_invent_calls(tmp_path):
    store = make_store(
        tmp_path,
        """
class A:
    def send(self): pass
def receiver(client): client.send()
def entry(values): receiver(*values)
""",
    )
    assert direct(store, "receiver")[0][0]["kind"] == "unresolved"


def test_external_missing_method_is_not_fabricated(tmp_path):
    store = make_store(
        tmp_path,
        """
from sdk import make
def entry(): make().nonexistent()
""",
        """
class Client: pass
def make() -> Client: pass
""",
    )
    assert any(n["kind"] == "unresolved" for n, _ in direct(store, "entry"))


def test_parameter_changes_invalidate_inference(tmp_path):
    store = make_store(
        tmp_path,
        """
class A:
    def send(self): pass
class B:
    def send(self): pass
def receiver(client): client.send()
def entry(): receiver(A())
""",
    )
    assert direct(store, "receiver")[0][0]["qualified_name"] == "main.A.send"
    path = store.root / "main.py"
    path.write_text(path.read_text().replace("receiver(A())", "receiver(B())"))
    assert {n["qualified_name"] for n, _ in direct(store, "receiver")} == {
        "main.B.send"
    }


def test_known_external_type_without_installed_library_is_an_interface(tmp_path):
    store = make_store(
        tmp_path,
        """
from unavailable_sdk import Client
def entry(client: Client): client.send()
""",
    )
    node, edge = direct(store, "entry")[0]
    assert node["qualified_name"] == "unavailable_sdk.Client.send"
    assert node["kind"] == "external" and not node["doc"]
    assert edge["status"] == "candidate"
    assert edge["evidence"][0]["kind"] == "external-type-contract"


def test_nested_calls_at_same_column_count_as_separate_callsites(tmp_path):
    store = make_store(
        tmp_path,
        """
def leaf(): pass
def choose(): return leaf
def entry(): choose()()
""",
    )
    assert store.stats()["callsites"] == 2


def test_propagation_limits_are_visible(tmp_path, monkeypatch):
    from callmap.flow import FlowInference

    monkeypatch.setattr(FlowInference, "MAX_ROUNDS", 1)
    store = make_store(
        tmp_path,
        """
class A:
    def send(self): pass
def first(x): second(x)
def second(x): third(x)
def third(x): fourth(x)
def fourth(x): fifth(x)
def fifth(x): x.send()
def entry(): first(A())
""",
    )
    assert store.expand("entry")["inference_incomplete"]


def test_any_annotation_never_fabricates_a_typing_any_method(tmp_path):
    store = make_store(
        tmp_path,
        """
from typing import Any
def entry(client: Any): client.send()
""",
    )
    assert direct(store, "entry")[0][0]["kind"] == "unresolved"


def test_external_reexported_class_keeps_method_declarations(tmp_path):
    store = make_store(
        tmp_path,
        """
from sdk import Client
def entry(client: Client): client.send()
""",
    )
    library = tmp_path / "libs"
    package = library / "sdk"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .impl import Implementation as Client\n")
    (package / "impl.py").write_text(
        "class Implementation:\n    def send(self): pass\n"
    )
    store = GraphStore(store.root, library_roots=[str(library)])
    node, edge = direct(store, "entry")[0]
    assert node["qualified_name"] == "sdk.impl.Implementation.send"
    assert edge["evidence"][0]["kind"] == "external-declaration"
