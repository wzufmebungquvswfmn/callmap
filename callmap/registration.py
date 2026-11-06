"""Preview and apply user-scoped registrations for installed MCP clients."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

CLIENTS = ("codex", "claude", "opencode")
JSONC_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
TRAILING_COMMAS = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')


def read_json_config(path: Path) -> dict:
    """Accept JSONC comments/trailing commas without changing string contents."""
    if not path.exists():
        return {}
    text = JSONC_TOKENS.sub(
        lambda m: m[0] if m[0].startswith('"') else " ", path.read_text()
    )
    text = TRAILING_COMMAS.sub(lambda m: m[0] if m[0].startswith('"') else "", text)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f"配置必须是 JSON 对象: {path}")
    return value


def config_path(client: str) -> Path:
    if client == "codex":
        return (
            Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        )
    if client == "claude":
        directory = os.environ.get("CLAUDE_CONFIG_DIR")
        return (
            Path(directory) / ".claude.json"
            if directory
            else Path.home() / ".claude.json"
        )
    directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = Path(os.environ.get("OPENCODE_CONFIG_DIR", directory / "opencode"))
    jsonc = directory / "opencode.jsonc"
    return jsonc if jsonc.exists() else directory / "opencode.json"


def backup_config(path: Path) -> str | None:
    if not path.exists():
        return None
    with tempfile.NamedTemporaryFile(
        prefix=path.name + ".callmap-backup-", dir=path.parent, delete=False
    ) as file:
        file.write(path.read_bytes())
        return file.name


def write_json_config(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".callmap-",
            dir=path.parent,
            delete=False,
        ) as file:
            temporary = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_clients(
    clients: list[str] | None = None,
    name: str = "callmap",
    cache_root: str | None = None,
    apply: bool = False,
    replace: bool = False,
    *,
    source_roots: list[str] | None = None,
    excludes: list[str] | None = None,
    library_roots: list[str] | None = None,
    jedi: bool = False,
    scan_mode: str = "git",
) -> dict:
    """Use native client commands where possible; never expose existing config."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("注册名称只允许字母、数字、下划线及连字符")
    if importlib.util.find_spec("mcp") is None:
        raise ImportError("请先安装 MCP 支持：uv sync --extra mcp")
    requested = clients or ["all"]
    unknown = set(requested) - {*CLIENTS, "all"}
    if unknown:
        raise ValueError(f"不支持的客户端: {sorted(unknown)}")
    selected = list(CLIENTS) if "all" in requested else list(dict.fromkeys(requested))
    runtime = [str(Path(sys.executable).absolute()), "-m", "callmap"]
    if getattr(sys, "frozen", False):
        runtime = [str(Path(sys.executable).absolute())]
    if cache_root:
        runtime += ["--cache-dir", str(Path(cache_root).expanduser().resolve())]
    for option, values in (
        ("--source-root", source_roots),
        ("--exclude", excludes),
        ("--library-root", library_roots),
    ):
        for value in values or []:
            if option == "--library-root":
                value = str(Path(value).expanduser().resolve())
            runtime += [option, value]
    if jedi:
        runtime.append("--jedi")
    if scan_mode != "git":
        if scan_mode != "filesystem":
            raise ValueError("scan_mode 必须为 git 或 filesystem")
        runtime += ["--scan-mode", scan_mode]
    runtime.append("mcp")
    results = []
    for client in selected:
        executable = shutil.which(client)
        entry = {
            "client": client,
            "status": "preview",
            "config": str(config_path(client)),
        }
        if executable is None:
            entry["status"] = "not_installed"
            results.append(entry)
            continue
        if client == "codex":
            command = [executable, "mcp", "add", name, "--", *runtime]
            entry["command"] = command
        elif client == "claude":
            command = [
                executable,
                "mcp",
                "add",
                "--scope",
                "user",
                name,
                "--",
                *runtime,
            ]
            entry["command"] = command
        else:
            entry["fragment"] = {
                "mcp": {
                    name: {
                        "type": "local",
                        "command": runtime,
                        "enabled": True,
                        "timeout": 120000,
                    }
                }
            }
        if apply:
            backup = None
            path = config_path(client).expanduser().resolve()
            try:
                if client == "codex":
                    config = tomllib.loads(path.read_text()) if path.exists() else {}
                    existing = config.get("mcp_servers", {}).get(name)
                else:
                    config = read_json_config(path)
                    section = "mcpServers" if client == "claude" else "mcp"
                    mapping = config.get(section, {})
                    if not isinstance(mapping, dict):
                        raise ValueError(f"{section} 必须是对象")
                    existing = mapping.get(name)
                if existing is not None and not replace:
                    raise ValueError(f"已有 {name} 配置；使用 --replace 明确替换")
                backup = backup_config(path)
                if client == "opencode":
                    config.setdefault("mcp", {}).update(entry["fragment"]["mcp"])
                    write_json_config(path, config)
                else:
                    if client == "claude" and existing is not None:
                        subprocess.run(
                            [executable, "mcp", "remove", "--scope", "user", name],
                            capture_output=True,
                            check=True,
                            timeout=30,
                        )
                    subprocess.run(command, capture_output=True, check=True, timeout=30)
                entry["status"] = "registered"
                entry["backup"] = backup
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                if backup is not None:
                    shutil.copyfile(backup, path)
                entry["status"] = "error"
                entry["error"] = (
                    str(exc)
                    if isinstance(exc, (ValueError, OSError))
                    else "客户端注册命令失败或超时；已有配置已从备份恢复"
                )
                entry["backup"] = backup
        results.append(entry)
    return {
        "mode": "apply" if apply else "preview",
        "server_command": runtime,
        "mcpServers": {name: {"command": runtime[0], "args": runtime[1:]}},
        "clients": results,
        "next_step": "重新加载客户端；每次工具调用传入项目绝对路径 project_path",
    }
