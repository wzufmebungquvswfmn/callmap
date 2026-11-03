import json
import subprocess

import pytest

from callmap import registration


@pytest.fixture
def clients(tmp_path, monkeypatch):
    paths = {name: tmp_path / name / "config" for name in registration.CLIENTS}
    monkeypatch.setattr(registration, "config_path", lambda name: paths[name])
    monkeypatch.setattr(registration.shutil, "which", lambda name: "/fake/" + name)
    return paths


def test_preview_does_not_write_and_does_not_bind_project(clients):
    result = registration.register_clients()
    assert all(c["status"] == "preview" for c in result["clients"])
    assert "--project" not in result["server_command"]
    assert result["server_command"][-1] == "mcp"
    assert not any(path.exists() for path in clients.values())


def test_jsonc_preserves_string_values_and_other_settings(clients):
    path = clients["opencode"]
    path.parent.mkdir()
    original = (
        '{// comment\n"url":"https://example.test/a//b",'
        '"literal":"/*text*/,}", /* comment */'
        '"mcp":{"other":{"type":"remote","url":"https://other.test"},},}'
    )
    path.write_text(original)
    result = registration.register_clients(["opencode"], apply=True)
    entry = result["clients"][0]
    assert entry["status"] == "registered"
    config = json.loads(path.read_text())
    assert config["url"] == "https://example.test/a//b"
    assert config["literal"] == "/*text*/,}"
    assert config["mcp"]["other"]["url"] == "https://other.test"
    assert config["mcp"]["callmap"]["enabled"]
    from pathlib import Path

    assert Path(entry["backup"]).read_text() == original
    before = path.read_text()
    refused = registration.register_clients(["opencode"], apply=True)
    assert refused["clients"][0]["status"] == "error"
    assert path.read_text() == before
    replaced = registration.register_clients(["opencode"], apply=True, replace=True)
    assert replaced["clients"][0]["status"] == "registered"


def test_native_commands_and_rollback(clients, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert kwargs["capture_output"] and kwargs["check"]

    monkeypatch.setattr(registration.subprocess, "run", run)
    result = registration.register_clients(["codex", "claude"], apply=True)
    assert all(c["status"] == "registered" for c in result["clients"])
    assert commands[1][2:5] == ["add", "--scope", "user"]
    assert "--project" not in commands[0]
    path = clients["claude"]
    path.parent.mkdir()
    original = '{"mcpServers":{"callmap":{"command":"old"}},"other":123}'
    path.write_text(original)

    def fail(command, **kwargs):
        path.write_text("changed")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(registration.subprocess, "run", fail)
    failed = registration.register_clients(["claude"], apply=True, replace=True)
    assert failed["clients"][0]["status"] == "error"
    assert path.read_text() == original


def test_missing_client_is_skipped_and_invalid_config_untouched(clients, monkeypatch):
    monkeypatch.setattr(registration.shutil, "which", lambda name: None)
    result = registration.register_clients(["codex"], apply=True)
    assert result["clients"][0]["status"] == "not_installed"
    monkeypatch.setattr(registration.shutil, "which", lambda name: "/fake/" + name)
    path = clients["opencode"]
    path.parent.mkdir()
    path.write_text("invalid configuration")
    result = registration.register_clients(["opencode"], apply=True)
    assert result["clients"][0]["status"] == "error"
    assert path.read_text() == "invalid configuration"
