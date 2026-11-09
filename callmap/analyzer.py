"""Conservative AST call resolution. Never imports the analyzed application."""

from __future__ import annotations

import ast
import builtins
import hashlib
import inspect
import sys
import tokenize
from collections import defaultdict
from pathlib import Path

from callmap.scanning import is_test_path
from callmap.semantics import CallsiteMetadata, describe_edges
from callmap.stdlib_models import EXECUTOR_CALLBACKS


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def node_id(identity: str) -> str:
    return "n_" + digest(identity)[:24]


def expression(node: ast.AST | None) -> str:
    return ast.unparse(node) if node is not None else ""


def owned_walk(node: ast.AST):
    """Walk one lexical scope, without attributing nested bodies to its caller."""
    children = (
        [node.body]
        if isinstance(node, ast.Lambda)
        else (
            node.body
            if isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
            else ast.iter_child_nodes(node)
        )
    )
    for child in children:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.Lambda):
            continue
        yield child
        yield from owned_walk(child)


class Analyzer:
    def __init__(
        self,
        root: Path,
        files: list[Path],
        source_roots: list[Path],
        jedi: bool,
        docs=None,
    ):
        self.root = root
        self.files = files
        self.source_roots = source_roots
        self.use_jedi = jedi
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.symbols: dict[str, list[str]] = defaultdict(list)
        self.scopes: dict[str, dict] = {}
        self.modules: dict[str, str] = {}
        self.errors: list[dict] = []
        self.scripts: dict[str, object] = {}
        self.class_bases: dict[str, list[str]] = {}
        self.subclasses: dict[str, set[str]] = defaultdict(set)
        self.docs = docs
        self.flow = None
        self.lambda_ids: dict[int, str] = {}
        self.comprehension_names: dict[int, tuple[str, list[ast.AST]]] = {}
        self.comprehension_contexts: dict[int, tuple] = {}
        self.context_entry: str | None = None
        self.callsite_metadata = CallsiteMetadata(self)

    def analyze(self, context_entry: str | None = None) -> dict:
        for path in self.files:
            self._parse(path)
        if context_entry is not None:
            self.context_entry = next(
                n["id"] for n in self.nodes.values() if n["identity"] == context_entry
            )
        for sid, scope in self.scopes.items():
            if isinstance(scope["tree"], ast.ClassDef):
                self.class_bases[sid] = [
                    target
                    for base in scope["tree"].bases
                    for target, _ in self._resolve(base, scope["parent"], 10**9)
                    if target in self.scopes
                ]
        for sid, bases in self.class_bases.items():
            for base in bases:
                self.subclasses[base].add(sid)
        from callmap.docs import LibraryDocs
        from callmap.flow import INSTANCE_REASONS, UNCERTAIN_REASONS, FlowInference

        self.flow = FlowInference(self, self.docs or LibraryDocs([]))
        self.flow.propagate()
        phase_two = self.flow.summary()
        from callmap.registry import RegistryInference

        self.flow.registry = RegistryInference(self.flow)
        self.flow.propagate()
        for sid, scope in list(self.scopes.items()):
            if (
                self.flow.active_scopes is not None
                and sid not in self.flow.active_scopes
            ):
                continue
            if isinstance(scope["tree"], ast.ClassDef):
                # A constructor node points to its implementation once.
                for target in self._method(sid, "__init__", set()):
                    self._edge(sid, target, scope["tree"], "constructor", "resolved")
                continue
            for call in owned_walk(scope["tree"]):
                if not isinstance(call, ast.Call):
                    continue
                targets = self._resolve(call.func, sid, call.lineno)
                invoked = []
                for target, reason in targets:
                    node = self.nodes[target]
                    if reason in INSTANCE_REASONS and node["symbol_type"] == "class":
                        if target in self.scopes:
                            invoked.extend(
                                (
                                    method,
                                    reason
                                    if reason in {"argument_instance", "annotation"}
                                    else "instance",
                                )
                                for method in self._method(target, "__call__", set())
                            )
                    else:
                        invoked.append((target, reason))
                targets = invoked
                if not targets and self.use_jedi:
                    targets = self._jedi(call, scope)
                if not targets:
                    identity = f"unknown:{sid}:{expression(call.func)}"
                    target = node_id(identity)
                    self.nodes.setdefault(
                        target,
                        {
                            "id": target,
                            "identity": identity,
                            "name": expression(call.func),
                            "qualified_name": expression(call.func),
                            "kind": "unresolved",
                            "symbol_type": "unknown",
                            "path": scope["path"],
                            "line": call.lineno,
                            "end_line": call.end_lineno,
                            "signature": expression(call.func) + "(…)",
                            "doc": "",
                            "doc_source": "missing",
                            "code": "",
                            "hash": digest(identity + self.nodes[sid]["hash"]),
                        },
                    )
                    targets = [(target, "unresolved")]
                grouped: dict[str, set[str]] = defaultdict(set)
                for target, reason in targets:
                    grouped[target].add(reason)
                bindings = (
                    self.flow.callsite_bindings(sid, call)
                    if any(
                        reason in {"argument", "argument_instance", "bound_method"}
                        for _, reason in targets
                    )
                    and self.flow.parameter_dependencies(call.func, sid)
                    else []
                )
                if any(b["unknown"] for b in bindings):
                    unknown = self._unknown(sid, expression(call.func), call)
                    for binding in bindings:
                        if binding["unknown"]:
                            binding["target_ids"] = [unknown]
                    grouped[unknown].add("unresolved")
                for target, reasons in sorted(grouped.items()):
                    reason = sorted(reasons & UNCERTAIN_REASONS or reasons)[0]
                    status = (
                        "unresolved"
                        if reason == "unresolved"
                        else "candidate"
                        if len(set(t for t, _ in targets)) > 1
                        or reason in UNCERTAIN_REASONS
                        else "resolved"
                    )
                    self._edge(sid, target, call, reason, status)
                    submission = self.flow.task_submissions.get(
                        (sid, call.lineno, call.col_offset)
                    )
                    if submission:
                        self.edges[-1]["evidence"].append(
                            submission
                            | {
                                "target_id": target,
                                "test_source": is_test_path(submission["path"]),
                            }
                        )
                    if bindings:
                        self.edges[-1]["callsite_bindings"] = bindings
                        self.edges[-1]["callback_parameters"] = (
                            self.flow.parameter_dependencies(call.func, sid)
                        )
        unknown_callback_sites = set()
        for (source, line, column, target), record in self.flow.callbacks.items():
            callback_reason = (
                "executor-callback"
                if record["evidence"]["api"] in EXECUTOR_CALLBACKS
                else "execution-callback"
            )
            callback_call = ast.copy_location(
                ast.Call(func=record["callback"], args=[], keywords=[]),
                record["call"],
            )
            bindings = self.flow.callsite_bindings(source, callback_call)
            unknown = None
            if any(b["unknown"] for b in bindings):
                unknown = self._unknown(source, record["argument"], record["call"])
                for binding in bindings:
                    if binding["unknown"]:
                        binding["target_ids"] = [unknown]
            if target is None:
                target = self._unknown(source, record["argument"], record["call"])
            self._edge(
                source,
                target,
                record["call"],
                callback_reason,
                "unresolved"
                if self.nodes[target]["kind"] == "unresolved"
                else "candidate",
                evidence=[record["evidence"]]
                + self.flow.evidence(source, target, "argument", callback_call),
                label=expression(record["call"].func) + " [callback]",
                synthetic=True,
            )
            if bindings:
                self.edges[-1]["callsite_bindings"] = bindings
                self.edges[-1]["callback_parameters"] = (
                    self.flow.parameter_dependencies(record["callback"], source)
                )
            site = (source, line, column)
            if unknown and target != unknown and site not in unknown_callback_sites:
                unknown_callback_sites.add(site)
                self._edge(
                    source,
                    unknown,
                    record["call"],
                    callback_reason,
                    "unresolved",
                    evidence=[
                        record["evidence"]
                        | {
                            "target_id": unknown,
                        }
                    ]
                    + self.flow.evidence(source, unknown, "unresolved", callback_call),
                    label=expression(record["call"].func) + " [callback]",
                    synthetic=True,
                )
                self.edges[-1]["callsite_bindings"] = bindings
                self.edges[-1]["callback_parameters"] = (
                    self.flow.parameter_dependencies(record["callback"], source)
                )
        describe_edges(self.edges, self.nodes)
        return {
            "nodes": list(self.nodes.values()),
            "edges": self.edges,
            "errors": self.errors,
            "analysis": self.flow.summary()
            | {"type_phase": phase_two}
            | self.flow.registry.summary(),
            "context_scope": sorted(self.flow.active_scopes or []),
        }

    def _parse(self, path: Path) -> None:
        relative = path.relative_to(self.root).as_posix()
        base = next((p for p in self.source_roots if path.is_relative_to(p)), self.root)
        parts = list(path.relative_to(base).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        try:
            with tokenize.open(path) as stream:
                source = stream.read()
            tree = ast.parse(source, filename=relative)
        except (SyntaxError, UnicodeError, OSError) as exc:
            self.errors.append({"path": relative, "error": str(exc)})
            return
        lines = source.splitlines(keepends=True)
        mid = self._register(tree, "<module>", module, relative, lines, None, None)
        self.modules[module] = mid

        def visit(parent: ast.AST, parent_id: str, prefix: str, class_id: str | None):
            counts: dict[str, int] = defaultdict(int)

            def children(container: ast.AST):
                for child in ast.iter_child_nodes(container):
                    if isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        counts[child.name] += 1
                        suffix = (
                            "" if counts[child.name] == 1 else f"@{counts[child.name]}"
                        )
                        qual = f"{prefix}{child.name}{suffix}"
                        sid = self._register(
                            child, qual, module, relative, lines, parent_id, class_id
                        )
                        visit(
                            child,
                            sid,
                            qual + ".",
                            sid if isinstance(child, ast.ClassDef) else class_id,
                        )
                    elif isinstance(child, ast.Lambda):
                        counts["<lambda>"] += 1
                        qual = f"{prefix}<lambda>@{counts['<lambda>']}"
                        sid = self._register(
                            child, qual, module, relative, lines, parent_id, None
                        )
                        self.lambda_ids[id(child)] = sid
                        visit(child, sid, qual + ".", None)
                    else:
                        children(child)

            children(parent)

        visit(tree, mid, "", None)
        self._comprehension_bindings(tree, mid)

    def _comprehension_bindings(self, tree: ast.AST, sid: str) -> None:
        """Keep iteration bindings local, including closures created in the body."""
        scope_ids = {id(scope["tree"]): key for key, scope in self.scopes.items()}

        def walk(node, owner, bindings):
            if bindings:
                self.comprehension_contexts[id(node)] = tuple(
                    (name, value[0], tuple(id(item) for item in value[1]))
                    for name, value in sorted(bindings.items())
                )
            if isinstance(node, ast.Name) and node.id in bindings:
                self.comprehension_names[id(node)] = bindings[node.id]
            if isinstance(node, ast.Lambda):
                owner = self.lambda_ids[id(node)]
                scope = self.scopes[owner]
                for name, value in bindings.items():
                    if name not in scope["bindings"]:
                        scope["bindings"][name].append((0, "comprehension", value))
                bindings = {
                    name: value
                    for name, value in bindings.items()
                    if name not in scope["parameters"]
                }
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                owner = scope_ids[id(node)]
                bindings = {}
            if isinstance(
                node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
            ):
                local = bindings.copy()
                for generator in node.generators:
                    # The first iterable is evaluated outside the implicit scope.
                    walk(generator.iter, owner, local)
                    values = (
                        list(generator.iter.elts)
                        if isinstance(generator.iter, (ast.List, ast.Tuple, ast.Set))
                        else []
                    )
                    for target in ast.walk(generator.target):
                        if isinstance(target, ast.Name):
                            local[target.id] = (
                                owner,
                                values
                                if isinstance(generator.target, ast.Name)
                                else [],
                            )
                    for condition in generator.ifs:
                        walk(condition, owner, local)
                for value in (
                    [node.key, node.value]
                    if isinstance(node, ast.DictComp)
                    else [node.elt]
                ):
                    walk(value, owner, local)
                return
            for child in ast.iter_child_nodes(node):
                walk(child, owner, bindings)

        walk(tree, sid, {})

    def _register(self, tree, qual, module, path, lines, parent, class_id) -> str:
        identity = f"{path}::{qual}"
        sid = node_id(identity)
        is_module = isinstance(tree, ast.Module)
        is_class = isinstance(tree, ast.ClassDef)
        is_lambda = isinstance(tree, ast.Lambda)
        start = getattr(tree, "lineno", 1)
        end = getattr(tree, "end_lineno", len(lines))
        decorators = getattr(tree, "decorator_list", [])
        if decorators:
            start = min(start, *(d.lineno for d in decorators))
        code = "".join(lines[start - 1 : end])
        if is_lambda:
            code = ast.get_source_segment("".join(lines), tree) or expression(tree)
        signature = qual
        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            signature += f"({ast.unparse(tree.args)})"
            if getattr(tree, "returns", None):
                signature += " -> " + expression(tree.returns)
        qualified = module if is_module else f"{module}.{qual}".lstrip(".")
        self.nodes[sid] = {
            "id": sid,
            "identity": identity,
            "name": qual.split(".")[-1],
            "qualified_name": qualified,
            "kind": "internal",
            "is_test": is_test_path(path),
            "symbol_type": "module"
            if is_module
            else "class"
            if is_class
            else "lambda"
            if is_lambda
            else "function",
            "path": path,
            "line": start,
            "end_line": end,
            "signature": signature,
            "doc": "" if is_lambda else ast.get_docstring(tree) or "",
            "doc_source": "source",
            "code": code,
            "hash": digest(code),
        }
        self.symbols[qualified.split("@")[0]].append(sid)
        scope = {
            "tree": tree,
            "parent": parent,
            "class_id": class_id,
            "module": module,
            "path": path,
            "bindings": defaultdict(list),
            "parameters": set(),
        }
        self.scopes[sid] = scope
        if parent and not is_lambda:
            self.scopes[parent]["bindings"][tree.name].append((0, "symbol", sid))
        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = tree.args.posonlyargs + tree.args.args + tree.args.kwonlyargs
            args += [a for a in (tree.args.vararg, tree.args.kwarg) if a]
            for arg in args:
                kind = "annotation" if arg.annotation else "unknown"
                scope["bindings"][arg.arg].append((0, kind, arg.annotation))
                scope["parameters"].add(arg.arg)
            if tree.args.kwarg:
                scope["bindings"][tree.args.kwarg.arg] = [
                    (0, "instance", self._external("builtins.dict"))
                ]
            if tree.args.vararg:
                scope["bindings"][tree.args.vararg.arg] = [
                    (0, "instance", self._external("builtins.tuple"))
                ]
            decorators = {expression(d).split(".")[-1] for d in decorators}
            if class_id and tree.args.args and "staticmethod" not in decorators:
                first = tree.args.args[0].arg
                if first in {"self", "cls"}:
                    scope["bindings"][first] = [
                        (0, "symbol" if first == "cls" else "instance", class_id)
                    ]
        for item in owned_walk(tree):
            if isinstance(item, ast.Import):
                for alias in item.names:
                    name = alias.asname or alias.name.split(".")[0]
                    value = alias.name if alias.asname else name
                    scope["bindings"][name].append((item.lineno, "import", value))
            elif isinstance(item, ast.ImportFrom):
                package = module.split(".")
                if not path.endswith("/__init__.py") and path != "__init__.py":
                    package = package[:-1]
                if item.level:
                    package = package[: len(package) - item.level + 1]
                    origin = ".".join(package + ([item.module] if item.module else []))
                else:
                    origin = item.module or ""
                for alias in item.names:
                    scope["bindings"][alias.asname or alias.name].append(
                        (item.lineno, "import", f"{origin}.{alias.name}".strip("."))
                    )
            elif isinstance(item, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                targets = (
                    item.targets if isinstance(item, ast.Assign) else [item.target]
                )
                for target in targets:
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        value = item.value
                        kind = "value"
                        if value is None and isinstance(item, ast.AnnAssign):
                            value, kind = item.annotation, "annotation"
                        scope["bindings"][expression(target)].append(
                            (item.lineno, kind, value)
                        )
            elif isinstance(item, (ast.For, ast.AsyncFor)):
                for target in ast.walk(item.target):
                    if isinstance(target, ast.Name):
                        scope["bindings"][target.id].append(
                            (getattr(item, "lineno", 0), "unknown", None)
                        )
            elif isinstance(item, ast.withitem) and item.optional_vars:
                for target in ast.walk(item.optional_vars):
                    if isinstance(target, ast.Name):
                        scope["bindings"][target.id].append(
                            (0, "context_manager", item.context_expr)
                        )
        return sid

    def _lookup(self, name: str, sid: str, line: int, seen: set) -> list:
        scope = self.scopes[sid]
        contextual = {}
        if self.flow and self.flow.lookup_context:
            owner, arguments = self.flow.lookup_context
            if owner == sid:
                contextual = arguments
        bindings = scope["bindings"].get(name)
        if bindings is not None:
            result = (
                list(contextual[name])
                if name in contextual
                else list(self.flow.arguments.get((sid, name), []))
                if self.flow and name in scope["parameters"]
                else []
            )
            eligible = [b for b in bindings if b[0] <= line]
            # Multiple possible assignments are deliberately not flow-selected.
            uncertain = len(eligible) > 1
            for binding_line, kind, value in eligible:
                if (
                    (self.context_entry or name in contextual)
                    and name in scope["parameters"]
                    and (result or name in contextual)
                    and kind in {"annotation", "unknown", "instance", "symbol"}
                ):
                    continue
                if kind in {"symbol", "instance"}:
                    result.append(
                        (value, "instance" if kind == "instance" else "lexical")
                    )
                elif kind == "import":
                    result.extend(self._named(value, seen))
                elif kind == "context_manager":
                    from callmap.stdlib_models import EXECUTOR_CLASSES

                    receivers = self._resolve(value, sid, line, seen)
                    result.extend(
                        (ref, reason)
                        for ref, reason in receivers
                        if self.nodes[ref]["qualified_name"] in EXECUTOR_CLASSES
                    )
                    if not result:
                        enter = ast.Call(
                            func=ast.Attribute(value=value, attr="__enter__"),
                            args=[],
                            keywords=[],
                        )
                        result.extend(self._resolve(enter, sid, line, seen))
                elif kind == "comprehension":
                    owner, values = value
                    result.extend(
                        ref
                        for item in values
                        for ref in self._resolve(item, owner, 10**9, seen)
                    )
                elif kind in {"value", "annotation"} and value is not None:
                    refs = (
                        self._annotation(value, sid, seen)
                        if kind == "annotation"
                        else self._resolve(value, sid, binding_line - 1, seen)
                    )
                    result.extend(
                        (target, "annotation" if kind == "annotation" else reason)
                        for target, reason in refs
                    )
            if uncertain:
                from callmap.flow import INSTANCE_REASONS

                result = [
                    (
                        target,
                        "argument_instance"
                        if reason in INSTANCE_REASONS
                        and self.nodes[target]["symbol_type"] == "class"
                        else reason,
                    )
                    for target, reason in result
                ]
            return result
        parent = scope["parent"]
        # Class namespaces are not lexical enclosing scopes of methods.
        while parent and isinstance(self.scopes[parent]["tree"], ast.ClassDef):
            parent = self.scopes[parent]["parent"]
        if parent:
            return self._lookup(name, parent, 10**9, seen)
        if callable(getattr(builtins, name, None)):
            return [(self._external("builtins." + name), "builtin")]
        return []

    def _named(self, name: str, seen: set) -> list:
        if name in self.symbols:
            return [(sid, "import") for sid in self.symbols[name]]
        # Follow re-exports in package __init__.py without importing it.
        module, _, attr = name.rpartition(".")
        if module in self.modules:
            mid = self.modules[module]
            if attr in self.scopes[mid]["bindings"]:
                return self._resolve(ast.Name(id=attr), mid, 10**9, seen)
        if any(name == m or name.startswith(m + ".") for m in self.modules):
            # A missing internal symbol must never masquerade as an external API.
            return []
        return [(self._external(name), "import")]

    def _resolve(self, expr, sid, line, seen=None) -> list[tuple[str, str]]:
        if expr is None or not sid:
            return []
        key = (
            sid,
            line,
            expression(expr),
            self.comprehension_contexts.get(id(expr), ()),
        )
        seen = set() if seen is None else seen
        if key in seen or len(seen) > 30:
            return []
        seen = seen | {key}
        if (
            self.flow
            and self.flow.registry
            and isinstance(expr, (ast.Call, ast.Subscript))
        ):
            registry_values = self.flow.registry.lookup(expr, sid)
            if registry_values is not None:
                return registry_values
        if isinstance(expr, ast.Name):
            if id(expr) in self.comprehension_names:
                owner, values = self.comprehension_names[id(expr)]
                return [
                    ref
                    for value in values
                    for ref in self._resolve(value, owner, line, seen)
                ]
            return self._lookup(expr.id, sid, line, seen)
        if isinstance(expr, ast.Lambda):
            target = self.lambda_ids.get(id(expr))
            return [(target, "lexical")] if target else []
        literal_types = {
            ast.Dict: "dict",
            ast.List: "list",
            ast.Set: "set",
            ast.Tuple: "tuple",
        }
        literal_type = literal_types.get(type(expr))
        if isinstance(expr, ast.Constant) and expr.value is not None:
            literal_type = type(expr.value).__name__
        if literal_type:
            return [(self._external("builtins." + literal_type), "instance")]
        if isinstance(expr, ast.Await):
            return self._resolve(expr.value, sid, line, seen)
        if isinstance(expr, (ast.BoolOp, ast.IfExp)):
            parts = (
                expr.values
                if isinstance(expr, ast.BoolOp)
                else [expr.body, expr.orelse]
            )
            return [
                (
                    target,
                    "argument_instance"
                    if self.nodes[target]["symbol_type"] == "class"
                    and reason
                    in {
                        "instance",
                        "annotation",
                        "external_instance",
                        "argument_instance",
                    }
                    else "bound_method"
                    if reason == "bound_method"
                    else "inference",
                )
                for part in parts
                for target, reason in self._resolve(part, sid, line, seen)
            ]
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return [
                (t, "annotation")
                for e in (expr.left, expr.right)
                for t, _ in self._resolve(e, sid, line, seen)
            ]
        if isinstance(expr, ast.Subscript):
            if expression(expr.value).split(".")[-1] in {
                "dict",
                "list",
                "set",
                "tuple",
                "frozenset",
            }:
                return [
                    (target, "annotation")
                    for target, _ in self._resolve(expr.value, sid, line, seen)
                ]
            if expression(expr.value).split(".")[-1] in {
                "Optional",
                "Union",
                "Annotated",
            }:
                parts = (
                    expr.slice.elts
                    if isinstance(expr.slice, ast.Tuple)
                    else [expr.slice]
                )
                return [
                    (t, "annotation")
                    for e in parts
                    for t, _ in self._resolve(e, sid, line, seen)
                ]
            return []
        if isinstance(expr, ast.Attribute):
            scope = self.scopes[sid]
            field = expression(expr)
            if field in scope["bindings"]:
                return self._lookup(field, sid, line, seen)
            result = []
            for base, reason in self._resolve(expr.value, sid, line, seen):
                node = self.nodes[base]
                if node["kind"] == "external":
                    from callmap.flow import INSTANCE_REASONS

                    if (
                        self.flow
                        and reason in INSTANCE_REASONS
                        and not node["qualified_name"].startswith("builtins.")
                    ):
                        qualified = self.flow.external_method(
                            node["qualified_name"] + "." + expr.attr
                        )
                        if qualified:
                            result.append(
                                (self._external(qualified), "external_return")
                            )
                        continue
                    if node["qualified_name"].startswith("builtins."):
                        obj = builtins
                        for part in node["qualified_name"].split(".")[1:]:
                            obj = getattr(obj, part, None)
                        if not hasattr(obj, expr.attr):
                            continue
                    result.extend(
                        (t, reason)
                        for t, _ in self._named(
                            node["qualified_name"] + "." + expr.attr, seen
                        )
                    )
                elif node["symbol_type"] == "module":
                    qualified = node["qualified_name"] + "." + expr.attr
                    result.extend(
                        [(target, "import") for target in self.symbols[qualified]]
                        if qualified in self.symbols
                        else self._resolve(ast.Name(id=expr.attr), base, 10**9, seen)
                    )
                elif node["symbol_type"] == "class":
                    if reason.startswith(("super_instance:", "super_class:")):
                        role, start = reason.split(":", 1)
                        result.extend(
                            self._super_methods(
                                base, start, expr.attr, role == "super_instance"
                            )
                        )
                        continue
                    methods = self._method(base, expr.attr, set())
                    if reason in {
                        "annotation",
                        "inference",
                        "instance",
                        "argument_instance",
                    } and not (self.context_entry and reason != "annotation"):
                        for child in self._descendants(base):
                            methods.extend(self._method(child, expr.attr, set()))
                        methods = sorted(set(methods))
                    for target in methods:
                        method_reason = reason
                        if self.flow and self.flow.is_bound(target, expr, sid):
                            method_reason = "bound_method"
                            self.flow.method_receivers[target].add((base, reason))
                        result.append((target, method_reason))
                    if not methods:
                        for init in self._method(base, "__init__", set()):
                            field_name = "self." + expr.attr
                            if field_name in self.scopes[init]["bindings"]:
                                result.extend(
                                    self._lookup(field_name, init, 10**9, seen)
                                )
            return result
        if isinstance(expr, ast.Call):
            if expression(expr.func) == "super" and any(
                self.nodes[target]["qualified_name"] == "builtins.super"
                for target, _ in self._resolve(expr.func, sid, line, seen)
            ):
                return self._super_receivers(expr, sid, line, seen)
            result = []
            for target, _ in self._resolve(expr.func, sid, line, seen):
                node = self.nodes[target]
                if self.flow and node["kind"] == "external":
                    result.extend(self.flow.external_returns(node["qualified_name"]))
                if node["symbol_type"] == "class":
                    result.append((target, "instance"))
                elif target in self.scopes:
                    tree = self.scopes[target]["tree"]
                    returns = getattr(tree, "returns", None)
                    if returns is not None:
                        result.extend(self._annotation(returns, target, seen))
                    if self.flow:
                        result.extend(self.flow.return_values(target, seen))
            return result
        return []

    def _annotation(self, value, sid, seen) -> list:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            try:
                value = ast.parse(value.value, mode="eval").body
            except SyntaxError:
                return []
        return [
            (target, "annotation")
            for target, _ in self._resolve(value, sid, 10**9, seen)
            if self.nodes[target]["qualified_name"]
            not in {"typing.Any", "typing_extensions.Any"}
        ]

    def _descendants(self, sid: str) -> set[str]:
        visited = {sid}
        queue = [sid]
        while queue:
            for child in self.subclasses.get(queue.pop(), set()):
                if child not in visited:
                    visited.add(child)
                    queue.append(child)
        return visited - {sid}

    def _mro(self, class_id: str, seen: set | None = None) -> list[str]:
        seen = seen or set()
        if class_id in seen:
            return []
        bases = self.class_bases.get(class_id, [])
        sequences = [self._mro(base, seen | {class_id}) for base in bases]
        if any(not sequence for sequence in sequences):
            return []
        sequences.append(list(bases))
        result = [class_id]
        while any(sequences):
            sequences = [sequence for sequence in sequences if sequence]
            head = next(
                (s[0] for s in sequences if all(s[0] not in t[1:] for t in sequences)),
                None,
            )
            if head is None:
                return []
            result.append(head)
            for sequence in sequences:
                if sequence[0] == head:
                    sequence.pop(0)
        return result

    def _super_receivers(self, call, sid, line, seen) -> list:
        if call.keywords or len(call.args) not in {0, 2}:
            return []
        if call.args:
            starts = [t for t, _ in self._resolve(call.args[0], sid, line, seen)]
            receivers = self._resolve(call.args[1], sid, line, seen)
        else:
            scope = self.scopes[sid]
            start = scope["class_id"]
            if not start:
                return []
            starts = [start]
            args = scope["tree"].args.posonlyargs + scope["tree"].args.args
            if not args:
                return []
            receivers = self._lookup(args[0].arg, sid, line, seen)
        from callmap.flow import INSTANCE_REASONS

        result = []
        for receiver, reason in receivers:
            if receiver not in self.class_bases:
                continue
            classes = {receiver}
            if reason in INSTANCE_REASONS and not self.context_entry:
                classes |= self._descendants(receiver)
            for cls in sorted(classes):
                for start in starts:
                    if start not in self._mro(cls):
                        continue
                    role = (
                        "super_instance"
                        if reason in INSTANCE_REASONS
                        else "super_class"
                    )
                    result.append((cls, f"{role}:{start}"))
        return sorted(set(result))

    def _super_methods(self, receiver, start, name, instance) -> list:
        mro = self._mro(receiver)
        if start not in mro:
            return []
        for base in mro[mro.index(start) + 1 :]:
            methods = [
                value
                for _, kind, value in self.scopes[base]["bindings"].get(name, [])
                if kind == "symbol"
            ]
            if not methods:
                continue
            result = []
            for target in methods:
                decorators = {
                    expression(d).split(".")[-1]
                    for d in self.scopes[target]["tree"].decorator_list
                }
                bound = (
                    instance or "classmethod" in decorators
                ) and "staticmethod" not in decorators
                if bound and self.flow:
                    self.flow.method_receivers[target].add(
                        (receiver, "instance" if instance else "lexical")
                    )
                result.append((target, "bound_method" if bound else "lexical"))
            return result
        return []

    def _method(self, class_id: str, name: str, seen: set) -> list[str]:
        if class_id in seen:
            return []
        scope = self.scopes[class_id]
        bindings = scope["bindings"].get(name, [])
        direct = [v for _, kind, v in bindings if kind == "symbol"]
        if direct:
            return direct
        return [
            method
            for base in self.class_bases.get(class_id, [])
            for method in self._method(base, name, seen | {class_id})
        ]

    def _external(self, qualified: str) -> str:
        sid = node_id("external:" + qualified)
        if sid not in self.nodes:
            doc, signature, source = "", qualified + "(…)", "missing"
            symbol_type = "interface"
            if qualified.startswith("builtins."):
                obj = builtins
                for part in qualified.split(".")[1:]:
                    obj = getattr(obj, part, None)
                if inspect.isclass(obj):
                    symbol_type = "class"
                doc = inspect.getdoc(obj) or ""
                source = "python-builtins"
                try:
                    signature = qualified + str(inspect.signature(obj))
                except (ValueError, TypeError):
                    pass
            self.nodes[sid] = {
                "id": sid,
                "identity": "external:" + qualified,
                "name": qualified.rsplit(".", 1)[-1],
                "qualified_name": qualified,
                "kind": "external",
                "symbol_type": symbol_type,
                "path": "",
                "line": 0,
                "end_line": 0,
                "signature": signature,
                "doc": doc,
                "doc_source": source,
                "code": "",
                "hash": digest(qualified),
            }
        return sid

    def _unknown(self, source: str, label: str, call: ast.Call) -> str:
        identity = f"unknown:{source}:{label}"
        target = node_id(identity)
        self.nodes.setdefault(
            target,
            {
                "id": target,
                "identity": identity,
                "name": label,
                "qualified_name": label,
                "kind": "unresolved",
                "symbol_type": "unknown",
                "path": self.scopes[source]["path"],
                "line": call.lineno,
                "end_line": call.end_lineno,
                "signature": label + "(…)",
                "doc": "",
                "doc_source": "missing",
                "code": "",
                "hash": digest(identity + self.nodes[source]["hash"]),
            },
        )
        return target

    def _edge(
        self,
        source,
        target,
        call,
        reason,
        status,
        *,
        evidence=None,
        label=None,
        synthetic=False,
    ) -> None:
        label = label or (
            expression(call.func) if isinstance(call, ast.Call) else "__init__"
        )
        line = call.lineno
        column = call.col_offset
        identity = f"{source}:{line}:{column}:{target}:{label}"
        items = (
            evidence
            if evidence is not None
            else self.flow.evidence(source, target, reason, call)
            if self.flow
            else []
        )
        items = [
            item | {"test_source": is_test_path(item.get("path", ""))} for item in items
        ]
        self.edges.append(
            {
                "id": "e_" + digest(identity)[:24],
                "source": source,
                "target": target,
                "line": line,
                "column": column,
                "expression": label,
                "status": status,
                "reason": reason,
                "synthetic": synthetic or reason == "constructor",
                "evidence": items,
                "callsite": self.callsite_metadata.describe(
                    source, target, call, reason, synthetic
                ),
            }
        )
        if self.flow:
            arguments = self.flow.edge_arguments(source, target, call)
            if arguments:
                self.edges[-1]["argument_bindings"] = arguments
            # Callable instances retain their value identity for context filters.
            values = {
                value
                for (_, value) in self.flow.sources_by_callee.get(source, {})
                if value == target
                or (
                    value in self.scopes
                    and self.nodes[value]["symbol_type"] == "class"
                    and target in self._method(value, "__call__", set())
                )
            }
            if values:
                self.edges[-1]["callback_value_ids"] = sorted(values)

    def _jedi(self, call: ast.Call, scope: dict) -> list:
        import jedi

        # A frozen executable cannot serve as Jedi's Python subprocess.
        # Use its embedded interpreter without searching for a system Python.
        environment = None
        if getattr(sys, "frozen", False):
            from jedi.api.environment import InterpreterEnvironment

            environment = InterpreterEnvironment()
        path = str(self.root / scope["path"])
        if path not in self.scripts:
            self.scripts[path] = jedi.Script(
                path=path,
                environment=environment,
                project=jedi.Project(
                    self.root,
                    added_sys_path=[str(p) for p in self.source_roots],
                    load_unsafe_extensions=False,
                ),
            )
        func = call.func
        if not isinstance(func, (ast.Name, ast.Attribute)):
            return []
        try:
            # AST columns are UTF-8 bytes; Jedi uses Unicode character offsets.
            source_line = (
                (self.root / scope["path"])
                .read_text()
                .splitlines()[func.end_lineno - 1]
            )
            column = len(source_line.encode()[: func.end_col_offset].decode())
            definitions = self.scripts[path].infer(func.end_lineno, column)
        except (ValueError, UnicodeError, RecursionError, AttributeError) as exc:
            self.errors.append(
                {"path": scope["path"], "line": call.lineno, "error": f"Jedi: {exc}"}
            )
            return []
        result = []
        for definition in definitions:
            qualified = definition.full_name
            if not qualified:
                continue
            if qualified in self.symbols:
                result.extend((t, "inference") for t in self.symbols[qualified])
            elif definition.module_path and Path(definition.module_path).is_relative_to(
                self.root
            ):
                continue
            else:
                target = self._external(qualified)
                self.nodes[target]["doc"] = definition.docstring()
                self.nodes[target]["doc_source"] = "jedi-static"
                result.append((target, "inference"))
        return result
