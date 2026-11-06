import pytest

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


@pytest.mark.parametrize("context_entry", [None, "entry"])
@pytest.mark.parametrize("super_expr", ["super()", "super(Child, self)"])
def test_super_override_has_no_false_recursion(tmp_path, context_entry, super_expr):
    store = make_store(
        tmp_path,
        f"""
class Base:
    def run(self, client): client.send()
class Child(Base):
    def run(self, client): {super_expr}.run(client)
class Client:
    def send(self): pass
def entry(): Child().run(Client())
""",
    )
    graph = store.expand("Child.run", context_entry=context_entry)
    entry = graph["entry"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    methods = {
        nodes[edge["target"]]["qualified_name"]
        for edge in graph["edges"]
        if edge["source"] == entry and edge["expression"].endswith(".run")
    }
    assert methods == {"main.Base.run"}
    assert not any(edge["source"] == edge["target"] for edge in graph["edges"])
    assert "main.Client.send" in {node["qualified_name"] for node in graph["nodes"]}


def test_super_uses_cooperative_multiple_inheritance_order(tmp_path):
    store = make_store(
        tmp_path,
        """
class Root:
    def run(self): pass
class Left(Root):
    def run(self): super().run()
class Right(Root):
    def run(self): super().run()
class Child(Left, Right):
    def run(self): super().run()
def entry(): Child().run()
""",
    )
    assert {
        node["qualified_name"]
        for node, edge in direct(store, "Child.run")
        if edge["expression"] == "super().run"
    } == {"main.Left.run"}
    assert {
        node["qualified_name"]
        for node, edge in direct(store, "Left.run")
        if edge["expression"] == "super().run"
    } == {"main.Root.run", "main.Right.run"}
    narrowed = store.expand("Left.run", context_entry="entry")
    nodes = {node["id"]: node for node in narrowed["nodes"]}
    assert {
        nodes[edge["target"]]["qualified_name"]
        for edge in narrowed["edges"]
        if edge["source"] == narrowed["entry"] and edge["expression"] == "super().run"
    } == {"main.Right.run"}


@pytest.mark.parametrize("decorator", ["", "    @classmethod\n"])
def test_super_proxy_preserves_binding_through_alias_and_parameter(tmp_path, decorator):
    first = "cls" if decorator else "self"
    store = make_store(
        tmp_path,
        f"""
class Client:
    def send(self): pass
class Base:
{decorator}    def run({first}, client): client.send()
class Child(Base):
{decorator}    def run({first}, client):
        proxy = super()
        forward(proxy, client)
def forward(proxy, client): proxy.run(client)
def entry(): Child().run(Client())
""",
    )
    graph = store.expand("entry", context_entry="entry")
    assert "main.Client.send" in {node["qualified_name"] for node in graph["nodes"]}
    assert not any(edge["source"] == edge["target"] for edge in graph["edges"])


@pytest.mark.parametrize("context_entry", [None, "entry"])
@pytest.mark.parametrize(
    "invocation",
    [
        "callback = Service().run; callback(Client())",
        "callback = Service().run; forward(callback, Client())",
        "choose().run(Client())",
        "choose_callback()(Client())",
        "callback = Service.class_method; forward(callback, Client())",
        "callback = Service.static; forward(callback, Client())",
        "callback = Service.run; callback(Service(), Client())",
        "pool.submit(Service().run, Client())",
        "callbacks = {'run': Service().run}; callbacks['run'](Client())",
    ],
)
def test_method_binding_survives_aliases_and_callbacks(
    tmp_path, context_entry, invocation
):
    store = make_store(
        tmp_path,
        f"""
from concurrent.futures import ThreadPoolExecutor
class Client:
    def send(self): pass
class Service:
    def run(self, client): client.send()
    @classmethod
    def class_method(cls, client): client.send()
    @staticmethod
    def static(client): client.send()
def forward(callback, client): callback(client)
def choose(): return Service()
def choose_callback(): return Service().run
def entry():
    pool = ThreadPoolExecutor()
    {invocation}
""",
    )
    graph = store.expand("entry", context_entry=context_entry)
    nodes = {node["id"]: node for node in graph["nodes"]}
    send_edges = [
        edge for edge in graph["edges"] if edge["expression"] == "client.send"
    ]
    assert send_edges
    assert {nodes[edge["target"]]["qualified_name"] for edge in send_edges} == {
        "main.Client.send"
    }
    assert not graph["inference_incomplete"]


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
