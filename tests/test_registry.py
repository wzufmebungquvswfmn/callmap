from test_inference import direct, make_store


def test_literal_registry_known_and_unknown_keys(tmp_path):
    store = make_store(
        tmp_path,
        """
def alpha(): pass
def beta(): pass
REGISTRY = {"a": alpha, "b": beta}
def known(): REGISTRY["a"]()
def unknown(key): REGISTRY.get(key)()
""",
    )
    assert {n["qualified_name"] for n, _ in direct(store, "known")} == {"main.alpha"}
    candidates = [
        (n, e) for n, e in direct(store, "unknown") if e["expression"].endswith("(key)")
    ]
    assert {n["qualified_name"] for n, _ in candidates} == {"main.alpha", "main.beta"}
    assert all(e["status"] == "candidate" and e["evidence"] for _, e in candidates)


def test_registration_calls_preserve_key_value_pairing(tmp_path):
    store = make_store(
        tmp_path,
        """
class A: pass
class B: pass
class Unregistered: pass
REGISTRY = {}
def register(key, implementation): REGISTRY[key] = implementation
register("a", A)
register("b", B)
def get():
    cls = REGISTRY.get("a")
    return cls()
""",
    )
    targets = [n for n, e in direct(store, "get") if e["expression"] == "cls"]
    assert {n["qualified_name"] for n in targets} == {"main.A"}


def test_bare_registration_decorator_and_property_keys(tmp_path):
    store = make_store(
        tmp_path,
        """
REGISTRY = {}
def key_of(cls):
    prop = getattr(cls, "kind", None)
    if prop:
        return prop.fget(cls)
def register(cls):
    key = key_of(cls)
    REGISTRY[key] = cls
    return cls
@register
class Alpha:
    @property
    def kind(self): return "alpha"
@register
class Beta:
    @property
    def kind(self): return "beta"
class Unregistered(Alpha): pass
def factory(key):
    cls = REGISTRY.get(key)
    return cls()
def selected():
    cls = REGISTRY.get("beta")
    return cls()
""",
    )
    targets = [(n, e) for n, e in direct(store, "factory") if e["expression"] == "cls"]
    assert {n["qualified_name"] for n, _ in targets} == {"main.Alpha", "main.Beta"}
    assert all(e["reason"] == "registry" for _, e in targets)
    assert {
        n["qualified_name"]
        for n, e in direct(store, "selected")
        if e["expression"] == "cls"
    } == {"main.Beta"}


def test_decorator_factory_and_factory_return_flow(tmp_path):
    store = make_store(
        tmp_path,
        """
REGISTRY = {}
def register(key):
    def decorate(cls):
        REGISTRY[key] = cls
        return cls
    return decorate
@register("alpha")
class Alpha: pass
def get_class(key): return REGISTRY.get(key)
def factory(key):
    cls = get_class(key)
    return cls()
""",
    )
    targets = [(n, e) for n, e in direct(store, "factory") if e["expression"] == "cls"]
    assert {n["qualified_name"] for n, _ in targets} == {"main.Alpha"}
    assert all(e["status"] == "candidate" for _, e in targets)


def test_registry_alias_across_modules_and_missing_key(tmp_path):
    store = make_store(
        tmp_path,
        """
from registry import HANDLERS as lookup
from registry import register
def work(): pass
register("work", work)
def factory(): lookup["work"]()
def missing(): lookup["missing"]()
""",
    )
    (store.root / "registry.py").write_text(
        "HANDLERS = {}\ndef register(key, value): HANDLERS[key] = value\n"
    )
    assert {n["qualified_name"] for n, _ in direct(store, "factory")} == {"main.work"}
    assert direct(store, "missing")[0][0]["kind"] == "unresolved"


def test_dynamic_key_registration_stays_candidate_for_known_lookup(tmp_path):
    store = make_store(
        tmp_path,
        """
REGISTRY = {}
class Alpha: pass
def register(key): REGISTRY[key] = Alpha
def factory(): REGISTRY["unknown"]()
""",
    )
    edges = direct(store, "factory")
    assert edges[0][0]["qualified_name"] == "main.Alpha"
    assert edges[0][1]["status"] == "candidate"
    assert edges[0][1]["evidence"][0]["key"] == "<dynamic>"


def test_registry_removal_invalidates_candidates(tmp_path):
    store = make_store(
        tmp_path,
        """
class Alpha: pass
class Beta: pass
REGISTRY = {"a": Alpha, "b": Beta}
def factory(key): REGISTRY[key]()
""",
    )
    assert {n["qualified_name"] for n, _ in direct(store, "factory")} == {
        "main.Alpha",
        "main.Beta",
    }
    path = store.root / "main.py"
    path.write_text(path.read_text().replace(', "b": Beta', ""))
    assert {n["qualified_name"] for n, _ in direct(store, "factory")} == {"main.Alpha"}


def test_known_callers_do_not_hide_dynamic_registry_targets(tmp_path):
    store = make_store(
        tmp_path,
        """
class Alpha: pass
class Beta: pass
REGISTRY = {"a": Alpha, "b": Beta}
def factory(key): REGISTRY[key]()
def entry(dynamic):
    factory("a")
    factory(dynamic)
""",
    )
    assert {n["qualified_name"] for n, _ in direct(store, "factory")} == {
        "main.Alpha",
        "main.Beta",
    }


def test_decorator_factory_preserves_captured_keys(tmp_path):
    store = make_store(
        tmp_path,
        """
REGISTRY = {}
def register(key):
    def decorate(cls):
        REGISTRY[key] = cls
        return cls
    return decorate
@register("a")
class Alpha: pass
@register("b")
class Beta: pass
def selected(): REGISTRY["b"]()
""",
    )
    assert {n["qualified_name"] for n, _ in direct(store, "selected")} == {"main.Beta"}
