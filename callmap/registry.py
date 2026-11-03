"""Static registry writes and lookups, with per-registration argument contexts."""

from __future__ import annotations

import ast
from collections import defaultdict

from callmap.analyzer import expression, owned_walk
from callmap.flow import INSTANCE_REASONS

ANY_KEY = object()


class RegistryInference:
    def __init__(self, flow):
        self.flow = flow
        self.a = flow.analyzer
        self.entries: dict[tuple, dict[object, set]] = defaultdict(
            lambda: defaultdict(set)
        )
        self.origins: dict[tuple, list[dict]] = defaultdict(list)
        self.writes = []
        self.initializers = []
        self.container_cache: dict[tuple, tuple | None] = {}
        self.key_parameters: set[tuple] = set()
        for sid, scope in self.a.scopes.items():
            for name, bindings in scope["bindings"].items():
                for line, kind, value in bindings:
                    if kind == "value" and isinstance(value, ast.Dict):
                        self.initializers.append(((sid, name), sid, value, line))
            for node in owned_walk(scope["tree"]):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    for target in targets:
                        if isinstance(target, ast.Subscript):
                            owner = self.container(target.value, sid)
                            if owner and node.value is not None:
                                self.writes.append(
                                    (owner, sid, target.slice, node.value, node.lineno)
                                )
                                self.mark_key_parameters(target.slice, sid)
                elif isinstance(node, ast.Call) and isinstance(
                    node.func, ast.Attribute
                ):
                    owner = self.container(node.func.value, sid)
                    if (
                        owner
                        and node.func.attr == "update"
                        and node.args
                        and isinstance(node.args[0], ast.Dict)
                    ):
                        self.initializers.append(
                            (owner, sid, node.args[0], node.lineno)
                        )
                    if owner and node.func.attr == "get" and node.args:
                        self.mark_key_parameters(node.args[0], sid)
                elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                    if self.container(node.value, sid):
                        self.mark_key_parameters(node.slice, sid)

    def mark_key_parameters(self, expr, sid, seen=None) -> None:
        if not sid:
            return
        seen = seen or set()
        for node in ast.walk(expr):
            if not isinstance(node, ast.Name) or (sid, node.id) in seen:
                continue
            key = (sid, node.id)
            seen = seen | {key}
            scope = self.a.scopes[sid]
            if node.id in scope["parameters"]:
                self.key_parameters.add(key)
            elif node.id in scope["bindings"]:
                for _, kind, value in scope["bindings"][node.id]:
                    if kind == "value" and value is not None:
                        self.mark_key_parameters(value, sid, seen)
            elif scope["parent"]:
                self.mark_key_parameters(node, scope["parent"], seen)

    def container(self, expr, sid, seen=None) -> tuple | None:
        if expr is None or not sid:
            return None
        key = (sid, expression(expr))
        seen = seen or set()
        if key in seen or len(seen) > 15:
            return None
        seen = seen | {key}
        scope = self.a.scopes[sid]
        name = expression(expr)
        bindings = scope["bindings"].get(name)
        if bindings is not None:
            for _, kind, value in bindings:
                if kind == "value" and isinstance(value, ast.Dict):
                    return sid, name
                if (
                    kind == "value"
                    and isinstance(value, ast.Call)
                    and expression(value.func) == "dict"
                ):
                    return sid, name
                if kind == "import":
                    module, _, attribute = value.rpartition(".")
                    if module in self.a.modules:
                        found = self.container(
                            ast.Name(id=attribute), self.a.modules[module], seen
                        )
                        if found:
                            return found
                if kind == "value" and isinstance(value, (ast.Name, ast.Attribute)):
                    found = self.container(value, sid, seen)
                    if found:
                        return found
            return None
        if isinstance(expr, ast.Name):
            parent = scope["parent"]
            while parent and isinstance(self.a.scopes[parent]["tree"], ast.ClassDef):
                parent = self.a.scopes[parent]["parent"]
            return self.container(expr, parent, seen) if parent else None
        if isinstance(expr, ast.Attribute):
            for target, _ in self.a._resolve(expr.value, sid, 10**9):
                if target in self.a.scopes:
                    found = self.container(ast.Name(id=expr.attr), target, seen)
                    if found:
                        return found
        return None

    def refs(self, expr, sid, context=None) -> list:
        context = context or {}
        if isinstance(expr, ast.Name) and expr.id in context.get("arguments", {}):
            return context["arguments"][expr.id]
        return self.flow.resolve(expr, sid, getattr(expr, "lineno", 10**9))

    def literals(self, expr, sid, context=None, seen=None) -> set:
        if expr is None or not sid:
            return set()
        context = context or {}
        seen = seen or set()
        key = (sid, expression(expr))
        if key in seen or len(seen) > 12:
            return set()
        seen = seen | {key}
        if isinstance(expr, ast.Constant):
            return (
                {expr.value}
                if isinstance(expr.value, (str, int, float, bool, type(None)))
                else set()
            )
        if isinstance(expr, ast.Name):
            if expr.id in context.get("constants", {}):
                return set(context["constants"][expr.id])
            scope = self.a.scopes[sid]
            if expr.id in scope["parameters"]:
                return set(self.flow.constants.get((sid, expr.id), set()))
            if expr.id in scope["bindings"]:
                return {
                    value
                    for _, kind, node in scope["bindings"][expr.id]
                    if kind == "value" and node is not None
                    for value in self.literals(node, sid, context, seen)
                }
            parent = scope["parent"]
            captured = context.get("closures", {}).get(parent, context)
            return self.literals(expr, parent, captured, seen) if parent else set()
        if isinstance(expr, ast.Attribute):
            values = set()
            for target, _ in self.refs(expr.value, sid, context):
                if target not in self.a.scopes:
                    continue
                scope = self.a.scopes[target]
                for _, kind, value in scope["bindings"].get(expr.attr, []):
                    if kind == "value":
                        values.update(self.literals(value, target, {}, seen))
                for method in (
                    self.a._method(target, expr.attr, set())
                    if isinstance(scope["tree"], ast.ClassDef)
                    else []
                ):
                    tree = self.a.scopes[method]["tree"]
                    if any(
                        expression(d).split(".")[-1] == "property"
                        for d in tree.decorator_list
                    ):
                        for value in self.flow.returns.get(method, []):
                            values.update(self.literals(value, method, {}, seen))
            return values
        if isinstance(expr, (ast.BoolOp, ast.IfExp)):
            parts = (
                expr.values
                if isinstance(expr, ast.BoolOp)
                else [expr.body, expr.orelse]
            )
            return {
                v for part in parts for v in self.literals(part, sid, context, seen)
            }
        if isinstance(expr, ast.Call):
            # Static property-getter pattern: getattr(cls, "key").fget(cls).
            if isinstance(expr.func, ast.Attribute) and expr.func.attr == "fget":
                receiver = expr.func.value
                if isinstance(receiver, ast.Name):
                    bindings = self.a.scopes[sid]["bindings"].get(receiver.id, [])
                    receiver = next(
                        (v for _, k, v in bindings if k == "value"), receiver
                    )
                if (
                    isinstance(receiver, ast.Call)
                    and expression(receiver.func) == "getattr"
                    and len(receiver.args) >= 2
                ):
                    values = set()
                    for attr in self.literals(receiver.args[1], sid, context, seen):
                        if isinstance(attr, str):
                            values.update(
                                self.literals(
                                    ast.Attribute(value=receiver.args[0], attr=attr),
                                    sid,
                                    context,
                                    seen,
                                )
                            )
                    return values
            values = set()
            for target, _ in self.refs(expr.func, sid, context):
                if target not in self.flow.returns:
                    continue
                tree = self.a.scopes[target]["tree"]
                params = tree.args.posonlyargs + tree.args.args
                arguments, constants = {}, {}
                for param, arg in zip(params, expr.args):
                    arguments[param.arg] = self.refs(arg, sid, context)
                    constants[param.arg] = self.literals(arg, sid, context, seen)
                nested = {"arguments": arguments, "constants": constants}
                for value in self.flow.returns[target]:
                    values.update(self.literals(value, target, nested, seen))
            return values
        return set()

    def update(self) -> int:
        additions = 0
        for owner, sid, mapping, line in self.initializers:
            for key, value in zip(mapping.keys, mapping.values):
                if key is None:
                    other = self.container(value, sid)
                    if other:
                        for item_key, refs in list(self.entries[other].items()):
                            additions += self.record(owner, {item_key}, refs, sid, line)
                    continue
                additions += self.record(
                    owner, self.literals(key, sid), self.refs(value, sid), sid, line
                )
        for owner, sid, key, value, line in self.writes:
            contexts = list(self.flow.invocations_by_target[sid].values())
            if not contexts:
                contexts = [{}]
            for context in contexts:
                additions += self.record(
                    owner,
                    self.literals(key, sid, context),
                    self.refs(value, sid, context),
                    sid,
                    line,
                )
        return additions

    def record(self, owner, keys, refs, sid, line) -> int:
        additions = 0
        for target, reason in refs:
            role = "registry_instance" if reason in INSTANCE_REASONS else "registry"
            for key in keys or {ANY_KEY}:
                bucket = self.entries[owner][key]
                if (target, role) not in bucket:
                    bucket.add((target, role))
                    additions += 1
                proof = {
                    "kind": "registry-write",
                    "path": self.a.scopes[sid]["path"],
                    "line": line,
                    "registry": self.a.nodes[owner[0]]["identity"] + ":" + owner[1],
                    "key": key if key is not ANY_KEY else "<dynamic>",
                    "target": self.a.nodes[target]["qualified_name"],
                }
                if proof not in self.origins[(owner, target)]:
                    self.origins[(owner, target)].append(proof)
        return additions

    def lookup(self, expr, sid) -> list | None:
        default = None
        if isinstance(expr, ast.Subscript):
            receiver, key = expr.value, expr.slice
        elif (
            isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "get"
            and expr.args
        ):
            receiver, key = expr.func.value, expr.args[0]
            default = expr.args[1] if len(expr.args) > 1 else None
        else:
            return None
        owner = self.container(receiver, sid)
        if owner is None:
            return None
        # A graph rooted at a function has no concrete argument values. Observed
        # literal callers must not hide implementations reachable via other inputs.
        keys = set() if self.depends_on_parameter(key, sid) else self.literals(key, sid)
        entries = self.entries[owner]
        refs = set(entries.get(ANY_KEY, set()))
        if keys:
            for item in keys:
                refs.update(entries.get(item, set()))
        else:
            for values in entries.values():
                refs.update(values)
        if default is not None and (not keys or any(k not in entries for k in keys)):
            refs.update(self.refs(default, sid))
        return sorted(refs)

    def depends_on_parameter(self, expr, sid, seen=None) -> bool:
        if not sid:
            return False
        seen = seen or set()
        for node in ast.walk(expr):
            if not isinstance(node, ast.Name) or (sid, node.id) in seen:
                continue
            key = (sid, node.id)
            nested_seen = seen | {key}
            scope = self.a.scopes[sid]
            if node.id in scope["parameters"]:
                return True
            if node.id in scope["bindings"]:
                if any(
                    self.depends_on_parameter(value, sid, nested_seen)
                    for _, kind, value in scope["bindings"][node.id]
                    if kind == "value" and value is not None
                ):
                    return True
            elif self.depends_on_parameter(node, scope["parent"], nested_seen):
                return True
        return False

    def evidence(self, target: str) -> list[dict]:
        return [
            proof
            for (_, candidate), proofs in self.origins.items()
            if candidate == target
            for proof in proofs
        ][:12]

    def summary(self) -> dict:
        return {
            "mapped_containers": sum(bool(v) for v in self.entries.values()),
            "callable_registries": sum(
                any(
                    reason == "registry"
                    and self.a.nodes[target]["symbol_type"]
                    in {"class", "function", "interface"}
                    for refs in entries.values()
                    for target, reason in refs
                )
                for entries in self.entries.values()
            ),
            "registry_memberships": sum(
                len(refs)
                for entries in self.entries.values()
                for refs in entries.values()
            ),
        }
