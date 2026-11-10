"""Discover static entry points without importing or running project code."""

from __future__ import annotations

import ast
import tomllib
from collections import Counter
from pathlib import Path

from callmap.analyzer import owned_walk

FRAMEWORK_FACTORIES = {
    "fastapi.FastAPI",
    "fastapi.APIRouter",
    "flask.Flask",
    "flask.Blueprint",
    "celery.Celery",
    "typer.Typer",
}
FRAMEWORK_DECORATORS = {
    "celery.shared_task",
    "celery.task",
    "click.command",
    "click.group",
}
INSTANCE_DECORATORS = {
    "fastapi": {
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "options",
        "head",
        "trace",
        "api_route",
        "websocket",
    },
    "flask": {"route", "get", "post", "put", "delete", "patch"},
    "celery": {"task"},
    "typer": {"command", "callback"},
}


def is_main_guard(test: ast.AST) -> bool:
    return main_guard_value(test) is True


def main_guard_value(test: ast.AST) -> bool | None:
    """Evaluate a direct __name__ check for execution as the main module."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        value = main_guard_value(test.operand)
        return None if value is None else not value
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
        return None
    left, right = test.left, test.comparators[0]
    recognized = any(
        isinstance(name, ast.Name)
        and name.id == "__name__"
        and isinstance(value, ast.Constant)
        and value.value == "__main__"
        for name, value in ((left, right), (right, left))
    )
    return isinstance(test.ops[0], ast.Eq) if recognized else None


def discover_entry_points(root: Path, graph: dict) -> dict:
    """Rank explicit startup targets, recognized registrations and public roots."""
    nodes = {n["id"]: n for n in graph["nodes"] if n["kind"] == "internal"}
    incoming = Counter(e["target"] for e in graph["edges"] if e["source"] in nodes)
    outgoing = Counter(e["source"] for e in graph["edges"])
    module_edges: dict[str, list[dict]] = {}
    for edge in graph["edges"]:
        module_edges.setdefault(edge["source"], []).append(edge)
    locations = {
        (n["path"], n["line"]): n
        for n in nodes.values()
        if n["symbol_type"] == "function"
    }
    qualified: dict[str, list[dict]] = {}
    for node in nodes.values():
        qualified.setdefault(node["qualified_name"], []).append(node)
    entries: dict[str, dict] = {}
    warnings = []

    def add(node, kind, confidence, path, line, detail):
        if node.get("is_test"):
            return
        entry = entries.setdefault(
            node["id"],
            {
                key: node[key]
                for key in (
                    "id",
                    "qualified_name",
                    "path",
                    "line",
                    "signature",
                    "symbol_type",
                )
            },
        )
        entry.setdefault("confidence", confidence)
        if confidence == "explicit":
            entry["confidence"] = confidence
        reason = {"kind": kind, "path": path, "line": line, "detail": detail}
        if reason not in entry.setdefault("reasons", []):
            entry["reasons"].append(reason)
        entry["incoming_count"] = incoming[node["id"]]
        entry["outgoing_count"] = outgoing[node["id"]]

    config_path = root / "pyproject.toml"
    if config_path.is_file():
        try:
            config = tomllib.loads(config_path.read_text())
            project = config.get("project", {})
            if not isinstance(project, dict):
                warnings.append("pyproject.toml 的 project 配置不是表")
                project = {}
            for section in ("scripts", "gui-scripts"):
                scripts = project.get(section, {})
                if not isinstance(scripts, dict):
                    continue
                for name, value in scripts.items():
                    if not isinstance(value, str) or ":" not in value:
                        warnings.append(f"无法识别 project.{section}.{name}: {value}")
                        continue
                    module, symbol = value.split(":", 1)
                    matches = qualified.get(f"{module.strip()}.{symbol.strip()}", [])
                    matches = [n for n in matches if n["symbol_type"] == "function"]
                    if len(matches) == 1:
                        add(
                            matches[0],
                            "project-script",
                            "explicit",
                            "pyproject.toml",
                            None,
                            f"project.{section}.{name} = {value}",
                        )
                    else:
                        warnings.append(f"命令入口未纳入索引或不唯一: {name} = {value}")
        except (OSError, ValueError) as exc:
            warnings.append(f"无法读取 pyproject.toml 入口配置: {exc}")

    for module in nodes.values():
        if module["symbol_type"] != "module" or module.get("is_test"):
            continue
        try:
            tree = ast.parse(module["code"])
        except SyntaxError:
            continue
        path = module["path"]
        aliases = {}
        instances = {}
        # Only use module-level imports and direct factory assignments as proof.
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
            elif isinstance(statement, ast.ImportFrom) and statement.level == 0:
                for alias in statement.names:
                    aliases[alias.asname or alias.name] = (
                        f"{statement.module}.{alias.name}"
                    )

        def import_name(expression):
            dotted = ast.unparse(expression)
            first, separator, rest = dotted.partition(".")
            if first not in aliases:
                return ""
            return aliases[first] + (separator + rest if separator else "")

        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        instances.pop(target.id, None)
                if isinstance(value, ast.Call):
                    factory = import_name(value.func)
                    if factory in FRAMEWORK_FACTORIES:
                        for target in targets:
                            if isinstance(target, ast.Name):
                                instances[target.id] = factory.split(".")[0]

        guards = [
            item
            for item in owned_walk(tree)
            if isinstance(item, ast.If) and main_guard_value(item.test) is not None
        ]
        main_file = Path(path).name == "__main__.py"
        conditions = [item for item in owned_walk(tree) if isinstance(item, ast.If)]
        found_startup = False
        for edge in module_edges.get(module["id"], []):
            target = nodes.get(edge["target"])
            if not target or target["symbol_type"] not in {"function", "class"}:
                continue
            startup_conditions = []
            excluded = False
            for condition in conditions:
                branch = next(
                    (
                        branch
                        for branch, body in (
                            (True, condition.body),
                            (False, condition.orelse),
                        )
                        if any(s.lineno <= edge["line"] <= s.end_lineno for s in body)
                    ),
                    None,
                )
                if branch is None:
                    continue
                value = main_guard_value(condition.test)
                if value is not None and branch != value:
                    excluded = True
                    break
                startup_conditions.append(value)
            if excluded:
                continue
            guard = next(
                (
                    g
                    for g in guards
                    if any(
                        statement.lineno <= edge["line"] <= statement.end_lineno
                        for statement in (
                            g.body if main_guard_value(g.test) else g.orelse
                        )
                    )
                ),
                None,
            )
            if guard is not None or main_file:
                confidence = (
                    "explicit"
                    if edge["status"] == "resolved" and None not in startup_conditions
                    else "candidate"
                )
                kind = "main-guard" if guard is not None else "main-module"
                add(target, kind, confidence, path, edge["line"], edge["expression"])
                found_startup = True
        if (main_file or guards) and not found_startup:
            add(
                module,
                "main-module" if main_file else "main-guard",
                "explicit",
                path,
                guards[0].lineno if guards else 1,
                "模块启动代码；未定位到唯一内部函数调用",
            )

        for definition in ast.walk(tree):
            if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = min(
                [definition.lineno] + [d.lineno for d in definition.decorator_list]
            )
            node = locations.get((path, start))
            if node is None:
                continue
            for decorator in definition.decorator_list:
                func = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = import_name(func)
                family = None
                if name in FRAMEWORK_DECORATORS:
                    family = name.split(".")[0]
                elif isinstance(func, ast.Attribute) and isinstance(
                    func.value, ast.Name
                ):
                    receiver = instances.get(func.value.id)
                    if func.attr in INSTANCE_DECORATORS.get(receiver, set()):
                        family = receiver
                if family:
                    add(
                        node,
                        "framework-registration",
                        "candidate",
                        path,
                        decorator.lineno,
                        ast.unparse(decorator),
                    )

    for edge in graph["edges"]:
        for proof in edge.get("evidence", []):
            if proof.get("kind") != "registry-write":
                continue
            for node in qualified.get(proof.get("target"), []):
                if node["symbol_type"] in {"function", "class"}:
                    add(
                        node,
                        "registry-registration",
                        "candidate",
                        proof.get("path"),
                        proof.get("line"),
                        f"{proof.get('registry')} 注册键 {proof.get('key')}",
                    )

    for node in nodes.values():
        if node["symbol_type"] != "function" or incoming[node["id"]]:
            continue
        local_name = node["identity"].split("::", 1)[1]
        if "." in local_name or node["name"].startswith("_"):
            continue
        add(
            node,
            "no-internal-callers",
            "candidate",
            node["path"],
            node["line"],
            "当前索引中没有已知内部调用者；也可能是未使用的函数",
        )

    ordered = sorted(
        entries.values(),
        key=lambda entry: (
            entry["confidence"] != "explicit",
            all(r["kind"] == "no-internal-callers" for r in entry["reasons"]),
            entry["outgoing_count"] == 0,
            -entry["outgoing_count"],
            entry["qualified_name"],
            entry["id"],
        ),
    )
    explicit = [entry for entry in ordered if entry["confidence"] == "explicit"]
    for entry in ordered:
        entry["deployment_verified"] = False
        entry["deployment_note"] = (
            "静态入口/注册信息不能单独证明部署启动路径或完整线上入口。"
        )
        entry["entry_kinds"] = sorted(
            {
                "public_root"
                if reason["kind"] == "no-internal-callers"
                else "framework"
                if reason["kind"] == "framework-registration"
                else "registry"
                if reason["kind"] == "registry-registration"
                else "startup"
                for reason in entry["reasons"]
            }
        )
        if len(entry["entry_kinds"]) > 1 and "public_root" in entry["entry_kinds"]:
            entry["entry_kinds"].remove("public_root")
    return {
        "entries": ordered,
        "explicit_count": len(explicit),
        "candidate_count": len(ordered) - len(explicit),
        "auto_entry": explicit[0]["id"] if len(explicit) == 1 else None,
        "warnings": warnings,
    }
