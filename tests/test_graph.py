import pytest

from callmap.store import GraphStore


@pytest.mark.parametrize(
    "comprehension",
    [
        "[target() for target in callbacks]",
        "{target() for target in callbacks}",
        "{target: target() for target in callbacks}",
        "tuple(target() for target in callbacks)",
        "[target() for target in (lambda: None,)]",
        "[target() for target in callbacks for target in callbacks]",
    ],
)
def test_comprehension_target_does_not_shadow_outer_function(tmp_path, comprehension):
    (tmp_path / "main.py").write_text(
        "def target(): pass\ndef entry(callbacks):\n"
        f"    {comprehension}\n    target()\n"
    )
    store = GraphStore(tmp_path)
    graph = store.graph()
    source = store.select(graph, "entry")["id"]
    target = store.select(graph, "main.target")["id"]
    outer_calls = [
        edge
        for edge in graph["edges"]
        if edge["source"] == source and edge["expression"] == "target"
    ]
    assert any(edge["line"] == 4 and edge["target"] == target for edge in outer_calls)
    assert not any(
        edge["line"] == 3 and edge["target"] == target for edge in outer_calls
    )


def test_comprehension_lambda_captures_local_variable(tmp_path):
    (tmp_path / "main.py").write_text("""
def target(): pass
def callback(): pass
def forward(fn): fn()
def entry():
    [forward(lambda: target()) for target in (callback,)]
    target()
""")
    graph = GraphStore(tmp_path).expand("entry", context_entry="entry")
    nodes = {node["id"]: node for node in graph["nodes"]}
    closure_edges = [
        edge
        for edge in graph["edges"]
        if nodes[edge["source"]]["symbol_type"] == "lambda"
    ]
    assert {nodes[edge["target"]]["qualified_name"] for edge in closure_edges} == {
        "main.callback"
    }


def test_comprehension_same_line_and_enclosing_iterable(tmp_path):
    (tmp_path / "main.py").write_text("""
def callback(): pass
def target(): pass
def entry():
    [target() for target in (callback,)]; target()
    [callback() for callback in (callback,)]
""")
    store = GraphStore(tmp_path)
    graph = store.graph()
    source = store.select(graph, "entry")["id"]
    names = {node["id"]: node["qualified_name"] for node in graph["nodes"]}
    assert [
        names[edge["target"]]
        for edge in graph["edges"]
        if edge["source"] == source and edge["line"] == 5
    ] == ["main.callback", "main.target"]
    assert [
        names[edge["target"]]
        for edge in graph["edges"]
        if edge["source"] == source and edge["line"] == 6
    ] == ["main.callback"]


@pytest.fixture
def project(tmp_path):
    def write(path, content):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    write("pkg/__init__.py", "from .helpers import shared as exported\n")
    write("pkg/helpers.py", "def shared():\n    return 42\n")
    write(
        "pkg/main.py",
        """
from . import exported as leaf
from .helpers import shared
import os.path as paths

def left():
    shared()

def right():
    leaf()

def recursive(n):
    if n:
        recursive(n - 1)
    shared()

def entry():
    left()
    right()
    recursive(2)
    paths.join("a", "b")

class Base:
    def inherited(self):
        shared()

class Client(Base):
    def __init__(self):
        shared()
    def run(self):
        self.inherited()

class Other:
    def run(self):
        return 0

def methods():
    client = Client()
    client.run()

def typed(client: Client):
    client.run()

def unknown(callback):
    callback()

def shadow(shared):
    shared()

def nested():
    def child():
        shared()
    child()
""",
    )
    return tmp_path, write, GraphStore(tmp_path)


def calls(store, symbol):
    graph = store.graph()
    node = store.select(graph, symbol)
    nodes = {n["id"]: n for n in graph["nodes"]}
    return [
        (nodes[e["target"]], e) for e in graph["edges"] if e["source"] == node["id"]
    ]


def test_diamond_and_recursion_share_unique_nodes(project):
    _, _, store = project
    graph = store.expand("pkg.main.entry")
    ids = [n["id"] for n in graph["nodes"]]
    assert len(ids) == len(set(ids))
    leaf = store.select(graph, "pkg.helpers.shared")
    assert sum(e["target"] == leaf["id"] for e in graph["edges"]) == 3
    recursive = store.select(graph, "pkg.main.recursive")
    assert any(e["source"] == e["target"] == recursive["id"] for e in graph["edges"])
    assert not graph["truncated"]
    assert any(n["qualified_name"] == "os.path.join" for n in graph["nodes"])


def test_methods_inheritance_and_no_same_name_guessing(project):
    _, _, store = project
    graph = store.expand("methods")
    names = {n["qualified_name"] for n in graph["nodes"]}
    assert {
        "pkg.main.Client.run",
        "pkg.main.Base.inherited",
        "pkg.helpers.shared",
    } <= names
    assert "pkg.main.Other.run" not in names
    edge = next(e for n, e in calls(store, "typed") if n["name"] == "run")
    assert edge["status"] == "candidate"


def test_parameters_shadow_imports_and_callbacks_stay_unknown(project):
    _, _, store = project
    for name in ("shadow", "unknown"):
        edges = calls(store, name)
        assert len(edges) == 1
        assert edges[0][0]["kind"] == "unresolved"


def test_nested_function_bodies_are_not_attributed_to_outer(project):
    _, _, store = project
    assert [n["qualified_name"] for n, _ in calls(store, "nested")] == [
        "pkg.main.nested.child"
    ]
    assert [n["qualified_name"] for n, _ in calls(store, "nested.child")] == [
        "pkg.helpers.shared"
    ]


def test_change_delete_and_stable_id_invalidate_cache(project):
    root, write, store = project
    original = store.ensure()
    before = store.select(original, "pkg.helpers.shared")
    assert store.ensure()["cache_hit"]
    write("pkg/helpers.py", "\n\ndef shared():\n    return 43\n")
    updated = store.ensure()
    after = store.select(updated, "pkg.helpers.shared")
    assert not updated["cache_hit"]
    assert before["id"] == after["id"]
    assert before["hash"] != after["hash"]
    (root / "pkg/helpers.py").unlink()
    assert not store.ensure()["cache_hit"]
    assert not any(
        n["qualified_name"] == "pkg.helpers.shared" and n["kind"] == "internal"
        for n in store.graph()["nodes"]
    )


def test_manual_documentation_and_target_persist_then_expire(project):
    root, _, store = project
    unknown = calls(store, "unknown")[0][0]
    store.annotate(unknown["id"], "业务回调", ["pkg.helpers.shared"])
    reloaded = GraphStore(root)
    node, edge = calls(reloaded, "unknown")[0]
    assert node["qualified_name"] == "pkg.helpers.shared"
    assert edge["status"] == "manual"
    path = root / "pkg/main.py"
    path.write_text(
        path.read_text().replace(
            "def unknown(callback):", "def unknown(callback, new=None):"
        )
    )
    node, edge = calls(reloaded, "unknown")[0]
    assert node["kind"] == "unresolved"
    assert node["annotation"]["stale"]


def test_budget_and_truncation_are_explicit(project):
    _, _, store = project
    assert store.expand("entry", depth=1)["truncated"]
    graph = store.expand("entry", max_nodes=2)
    assert graph["truncated"] and len(graph["nodes"]) == 2
    text = store.context("entry", budget=600, include_code=True)
    assert len(text) <= 600
    assert "字符预算已达上限" in text
    assert text.count("```") % 2 == 0


def test_duplicate_short_name_requires_disambiguation(project):
    _, _, store = project
    with pytest.raises(ValueError, match="不唯一"):
        store.expand("run")


def test_syntax_error_exclusion_and_no_application_execution(project):
    root, write, _ = project
    write("broken.py", "def bad(:")
    write("excluded.py", "def excluded(): pass")
    write("danger.py", "raise RuntimeError('must never execute')\ndef safe(): pass\n")
    write(".venv/lib/ignore.py", "def hidden(): pass")
    store = GraphStore(root, excludes=["excluded.py"])
    result = store.ensure()
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == "broken.py"
    names = {n["qualified_name"] for n in result["nodes"]}
    assert "danger.safe" in names
    assert "excluded.excluded" not in names
    assert not any("ignore" in name for name in names)


def test_external_docs_static_only_and_manual_override(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    library = tmp_path / "libraries"
    library.mkdir()
    (root / "main.py").write_text("from sdk import send\ndef entry(): send('hi')\n")
    (library / "sdk.py").write_text(
        'raise RuntimeError("no import")\ndef send(text: str) -> bool:\n'
        '    """Send a message."""\n    return True\n'
    )
    store = GraphStore(root, library_roots=[str(library)])
    node, _ = calls(store, "entry")[0]
    assert node["doc"] == "Send a message."
    assert "text: str" in node["signature"]
    store.annotate(node["id"], "人工接口说明")
    assert calls(store, "entry")[0][0]["doc"] == "人工接口说明"
    assert store.ensure()["cache_hit"]
    (library / "sdk.py").write_text("def send(text, retry=1): pass\n")
    assert not store.ensure()["cache_hit"]
    assert calls(store, "entry")[0][0]["annotation"]["stale"]


def test_src_layout_and_forward_annotations(tmp_path):
    package = tmp_path / "src/pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "service.py").write_text(
        "class Service:\n    def run(self): pass\n"
        'def entry(s: "Service"):\n    s.run()\n'
    )
    store = GraphStore(tmp_path)
    assert (
        calls(store, "pkg.service.entry")[0][0]["qualified_name"]
        == "pkg.service.Service.run"
    )


def test_function_decorator_calls_not_in_function_body(project):
    _, write, store = project
    write("decorated.py", "def deco(x): return x\n@deco(1)\ndef decorated(): pass\n")
    assert calls(store, "decorated.decorated") == []


def test_unknown_attribute_does_not_fabricate_external(project):
    _, write, store = project
    write("missing.py", "import pkg.helpers\ndef entry2(): pkg.helpers.missing()\n")
    assert calls(store, "entry2")[0][0]["kind"] == "unresolved"


def test_jedi_optional_resolves_returned_external_instance(tmp_path):
    pytest.importorskip("jedi")
    (tmp_path / "main.py").write_text(
        "from pathlib import Path\ndef entry():\n"
        '    path = Path(".")\n    path.exists()\n'
    )
    store = GraphStore(tmp_path, jedi=True)
    edges = calls(store, "entry")
    assert any(n["qualified_name"].endswith("Path.exists") for n, _ in edges)
    assert any(e["reason"] in {"inference", "external_return"} for _, e in edges)


def test_import_package_submodule_is_resolved(project):
    _, write, store = project
    write("qualified.py", "import pkg.helpers\ndef entry3(): pkg.helpers.shared()\n")
    assert calls(store, "entry3")[0][0]["qualified_name"] == "pkg.helpers.shared"


def test_literals_are_not_function_aliases_and_dict_methods_have_docs(project):
    _, write, store = project
    write(
        "values.py",
        """
from pkg.helpers import shared

def literal():
    callback = "shared"
    callback()

def dictionaries(data: dict[str, str], **kwargs):
    data.get("a")
    kwargs.keys()
    local = {}
    local.update(data)
""",
    )
    assert calls(store, "literal")[0][0]["kind"] == "unresolved"
    nodes = [n for n, _ in calls(store, "dictionaries")]
    assert {n["qualified_name"] for n in nodes} == {
        "builtins.dict.get",
        "builtins.dict.keys",
        "builtins.dict.update",
    }
    assert all(n["doc"] for n in nodes)


def test_subclass_dispatch_candidates_and_deduplicated_edges(project):
    _, write, store = project
    write(
        "dispatch.py",
        """
class Base:
    def run(self): pass
class First(Base):
    def run(self): pass
class Second(First): pass

def invoke(client: Base):
    client.run()
""",
    )
    edges = calls(store, "invoke")
    assert {n["qualified_name"] for n, _ in edges} == {
        "dispatch.Base.run",
        "dispatch.First.run",
    }
    assert all(e["status"] == "candidate" for _, e in edges)
    graph = store.expand("invoke")
    assert len(graph["edges"]) == len({e["id"] for e in graph["edges"]})
