import json
import subprocess
import sys

import pytest

from callmap.store import GraphStore


def make_project(tmp_path, files, **options):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return GraphStore(tmp_path, **options)


def entries_by_name(store):
    return {e["qualified_name"]: e for e in store.entries(limit=2000)["entries"]}


def test_config_and_main_module_merge_same_target_without_running_code(tmp_path):
    store = make_project(
        tmp_path,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/cli.py": "def main(): helper()\ndef helper(): pass\n",
            "src/pkg/__main__.py": "from .cli import main as launch\nlaunch()\n",
            "pyproject.toml": '[project.scripts]\napp = "pkg.cli:main"\n'
            '[project.gui-scripts]\ngui = "pkg.cli:main"\n',
        },
    )
    result = store.entries()
    assert result["explicit_count"] == 1
    entry = entries_by_name(store)["pkg.cli.main"]
    assert result["auto_entry"] == entry["id"]
    assert {r["kind"] for r in entry["reasons"]} == {"project-script", "main-module"}
    assert len(entry["reasons"]) == 3
    assert store.select(store.expand(result["auto_entry"]), "pkg.cli.helper")
    # Discovery is persisted in the same SQLite snapshot and reused on restart.
    assert GraphStore(tmp_path).entries()["cache_hit"]
    assert store.ensure()["entry_points"]["auto_entry"] == entry["id"]


def test_main_guard_calls_aliases_and_ignores_else_and_function_bodies(tmp_path):
    store = make_project(
        tmp_path,
        {
            "workers.py": "def run(): pass\ndef ignored(): pass\n",
            "launch.py": "from workers import run as start, ignored\n"
            "if '__main__' == __name__:\n    start()\nelse:\n    ignored()\n"
            "def nested():\n    if __name__ == '__main__': ignored()\n",
        },
    )
    result = store.entries()
    assert result["explicit_count"] == 1
    entry = entries_by_name(store)["workers.run"]
    assert result["auto_entry"] == entry["id"]
    assert entry["reasons"][0]["path"] == "launch.py"
    assert entry["reasons"][0]["line"] == 3
    assert "workers.ignored" not in entries_by_name(store)


@pytest.mark.parametrize(
    "guard, main_branch",
    [("__name__ == '__main__'", True), ("__name__ != '__main__'", False)],
)
def test_main_module_excludes_import_only_branches(tmp_path, guard, main_branch):
    body, other = ("main", "imported") if main_branch else ("imported", "main")
    store = make_project(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/__main__.py": "def main(): pass\ndef imported(): pass\n"
            f"if {guard}:\n    {body}()\nelse:\n    {other}()\n",
        },
    )
    result = store.entries()
    entry = entries_by_name(store)["pkg.__main__.main"]
    assert result["explicit_count"] == 1
    assert result["auto_entry"] == entry["id"]
    assert "pkg.__main__.imported" not in entries_by_name(store)


def test_main_module_unknown_condition_is_candidate(tmp_path):
    store = make_project(
        tmp_path,
        {
            "pkg/__main__.py": "def main(): pass\ndef conditional(): pass\n"
            "main()\nif enabled:\n    conditional()\n",
        },
    )
    entries = entries_by_name(store)
    assert entries["pkg.__main__.main"]["confidence"] == "explicit"
    assert entries["pkg.__main__.conditional"]["confidence"] == "candidate"
    assert store.entries()["auto_entry"] == entries["pkg.__main__.main"]["id"]


def test_multiple_explicit_entries_and_module_fallback(tmp_path):
    store = make_project(
        tmp_path,
        {
            "first.py": "def one(): pass\nif __name__ == '__main__': one()\n",
            "pkg/__main__.py": "print('not executed')\n",
        },
    )
    result = store.entries(limit=1)
    assert result["explicit_count"] == 2 and result["auto_entry"] is None
    assert result["next_offset"] == 1
    next_page = store.entries(offset=1)
    assert {
        result["entries"][0]["symbol_type"],
        next_page["entries"][0]["symbol_type"],
    } == {"module", "function"}


def test_framework_and_registry_entries_are_candidates(tmp_path):
    store = make_project(
        tmp_path,
        {
            "app.py": "from fastapi import FastAPI as API\n"
            "from celery import shared_task as task\n"
            "import click as cli\napp = API()\n"
            "@app.get('/hello')\nasync def hello(): helper()\n"
            "@task\ndef worker(): helper()\n"
            "@cli.command()\ndef command(): helper()\n"
            "def helper(): pass\n"
            "def registered(): helper()\nhandlers = {'work': registered}\n"
            "def dispatch(key): handlers[key]()\n",
        },
    )
    entries = entries_by_name(store)
    for name in ("hello", "worker", "command"):
        assert any(
            r["kind"] == "framework-registration"
            for r in entries[f"app.{name}"]["reasons"]
        )
        assert entries[f"app.{name}"]["confidence"] == "candidate"
    assert any(
        r["kind"] == "registry-registration"
        for r in entries["app.registered"]["reasons"]
    )
    assert store.entries()["auto_entry"] is None
    assert "app.helper" not in entries


def test_fallback_excludes_tests_private_methods_nested_and_unknown_get(tmp_path):
    store = make_project(
        tmp_path,
        {
            "app.py": "def leaf(): pass\ndef root(): leaf()\ndef unused(): pass\n"
            "def _private(): pass\n"
            "class Client:\n    def method(self): pass\n"
            "def outer():\n    def inner(): pass\n"
            "class Fake:\n    def get(self, path): return lambda fn: fn\n"
            "fake = Fake()\n@fake.get('/')\ndef view(): pass\n",
            "tests/test_fake.py": "def main(): pass\n"
            "if __name__ == '__main__': main()\n",
        },
    )
    result = store.entries()
    entries = entries_by_name(store)
    assert result["explicit_count"] == 0 and result["auto_entry"] is None
    assert "app.root" in entries and "app.unused" in entries
    assert result["entries"][0]["qualified_name"] == "app.root"
    assert not any(
        n in entries
        for n in (
            "app.leaf",
            "app._private",
            "app.Client.method",
            "app.outer.inner",
            "tests.test_fake.main",
        )
    )
    assert all(
        r["kind"] == "no-internal-callers" for r in entries["app.view"]["reasons"]
    )


def test_config_changes_and_missing_targets_are_visible(tmp_path):
    store = make_project(
        tmp_path,
        {
            "main.py": "def first(): pass\ndef second(): pass\n",
            "pyproject.toml": '[project.scripts]\napp = "main:first"\n',
        },
    )
    first = store.entries()["auto_entry"]
    (tmp_path / "pyproject.toml").write_text(
        '[project.scripts]\napp = "main:second"\nmissing = "missing:main"\n'
    )
    result = store.entries()
    assert not result["cache_hit"] and result["auto_entry"] != first
    assert result["warnings"] and "missing:main" in result["warnings"][0]
    filtered = GraphStore(
        tmp_path, tmp_path / "filtered", excludes=["main.py"]
    ).entries()
    assert filtered["explicit_count"] == 0 and filtered["warnings"]


@pytest.mark.parametrize("config", ["not valid TOML!", 'project = "invalid"'])
def test_bad_config_does_not_prevent_discovery(tmp_path, config):
    store = make_project(
        tmp_path, {"main.py": "def entry(): pass\n", "pyproject.toml": config}
    )
    assert store.entries()["warnings"]
    assert "main.entry" in entries_by_name(store)


def test_empty_project_invalid_paging_and_cli(tmp_path):
    store = make_project(tmp_path, {"data.py": "value = 1\n"})
    assert store.entries()["total"] == 0 and store.entries()["auto_entry"] is None
    for args in ({"limit": 0}, {"limit": 2001}, {"offset": -1}):
        with pytest.raises(ValueError):
            store.entries(**args)
    (tmp_path / "data.py").write_text(
        "def main(): pass\nif __name__ == '__main__': main()\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "callmap", "--project", str(tmp_path), "entries"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["explicit_count"] == 1
