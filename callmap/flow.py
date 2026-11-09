"""Bounded, context-insensitive propagation of observable argument/return types."""

from __future__ import annotations

import ast
from collections import defaultdict

from callmap.analyzer import expression, owned_walk
from callmap.stdlib_models import (
    CLASSES,
    COROUTINE_SCHEDULERS,
    EXECUTION_CALLBACKS,
    EXECUTOR_CALLBACKS,
    RETURNS,
)

INSTANCE_REASONS = {
    "instance",
    "annotation",
    "argument_instance",
    "external_instance",
    "registry_instance",
}
UNCERTAIN_REASONS = INSTANCE_REASONS | {
    "bound_method",
    "inference",
    "argument",
    "external_return",
    "return_flow",
    "registry",
    "external_class",
    "class_annotation",
}


def argument_reason(target: str, reason: str, nodes: dict) -> str:
    if reason == "bound_method" or reason.startswith(
        ("super_instance:", "super_class:")
    ):
        return reason
    if nodes[target]["symbol_type"] == "class" and reason in INSTANCE_REASONS:
        return "argument_instance"
    return "argument"


class FlowInference:
    MAX_ROUNDS = 16
    MAX_FACTS = 128

    def __init__(self, analyzer, docs):
        self.analyzer = analyzer
        self.docs = docs
        self.arguments: dict[tuple, set[tuple[str, str]]] = defaultdict(set)
        self.lookup_context: tuple[str, dict] | None = None
        self.method_receivers: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.active_scopes = (
            {analyzer.context_entry} if analyzer.context_entry is not None else None
        )
        self.callbacks: dict[tuple, dict] = {}
        self.task_submissions: dict[tuple, dict] = {}
        self.argument_sources: dict[tuple, set[tuple]] = defaultdict(set)
        self.sources_by_callee: dict[str, dict] = defaultdict(dict)
        self.observed_arguments: dict[tuple, set[tuple]] = defaultdict(set)
        self.call_sites = [
            (sid, call)
            for sid, scope in analyzer.scopes.items()
            if not isinstance(scope["tree"], ast.ClassDef)
            for call in owned_walk(scope["tree"])
            if isinstance(call, ast.Call)
        ]
        self.returns = {
            sid: [scope["tree"].body]
            if isinstance(scope["tree"], ast.Lambda)
            else [
                node.value
                for node in owned_walk(scope["tree"])
                if isinstance(node, ast.Return) and node.value is not None
            ]
            for sid, scope in analyzer.scopes.items()
            if isinstance(
                scope["tree"], (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            )
        }
        self.library_results: dict[str, list] = {}
        self.rounds = 0
        self.iterations: list[dict] = []
        self.converged = False
        self.saturated: set[tuple] = set()
        self.saturated_returns: set[str] = set()
        self.memo: dict[tuple, list] = {}
        self.return_cache: dict[str, list] = {}
        self.return_active: set[str] = set()
        self.previous_returns: dict[str, set] = defaultdict(set)
        self.return_additions = 0
        self.registry = None
        self.constants: dict[tuple, set] = defaultdict(set)
        self.invocations: dict[tuple, dict] = {}
        self.invocations_by_target: dict[str, dict] = defaultdict(dict)
        self.resolve_active: set[tuple] = set()
        self.decorator_sites = [
            (scope["parent"], decorator, scope["tree"])
            for scope in analyzer.scopes.values()
            if scope["parent"]
            for decorator in getattr(scope["tree"], "decorator_list", [])
        ]

    def resolve(self, expr, sid: str, line: int) -> list:
        key = (
            sid,
            line,
            expression(expr),
            self.analyzer.comprehension_contexts.get(id(expr), ()),
        )
        if key in self.resolve_active:
            return []
        if key not in self.memo:
            self.resolve_active.add(key)
            try:
                self.memo[key] = self.analyzer._resolve(expr, sid, line)
            finally:
                self.resolve_active.remove(key)
        return self.memo[key]

    def propagate(self) -> None:
        self.converged = False
        self.iterations = []
        for iteration in range(self.MAX_ROUNDS):
            self.memo.clear()
            self.return_cache.clear()
            self.return_additions = 0
            additions = 0
            for sid, call in self.call_sites:
                if self.active_scopes is not None and sid not in self.active_scopes:
                    continue
                for target, reason in self.resolve(call.func, sid, call.lineno):
                    scope = self.analyzer.scopes.get(target)
                    if scope is None:
                        additions += self.bind_executor(target, call, sid)
                        continue
                    additions += self.activate(target)
                    if isinstance(scope["tree"], ast.ClassDef):
                        if reason in INSTANCE_REASONS:
                            for invoke in self.analyzer._method(
                                target, "__call__", set()
                            ):
                                additions += self.activate(invoke)
                                additions += self.bind(
                                    invoke, call, sid, bound=self.instance_bound(invoke)
                                )
                            continue
                        for init in self.analyzer._method(target, "__init__", set()):
                            additions += self.activate(init)
                            additions += self.bind(init, call, sid, bound=True)
                    elif isinstance(
                        scope["tree"],
                        (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
                    ):
                        bound = self.is_bound(target, call.func, sid, reason)
                        additions += self.bind(target, call, sid, bound=bound)
            if self.registry:
                additions += self.bind_decorators()
                additions += self.registry.update()
            self.rounds = iteration + 1
            self.iterations.append(
                {
                    "round": self.rounds,
                    "new_bindings": additions,
                    "new_returns": self.return_additions,
                }
            )
            if not additions and not self.return_additions:
                self.converged = True
                break
        self.memo.clear()

    def activate(self, target: str) -> int:
        if self.active_scopes is not None and target not in self.active_scopes:
            self.active_scopes.add(target)
            return 1
        return 0

    def instance_bound(self, target: str) -> bool:
        return not any(
            expression(d).split(".")[-1] == "staticmethod"
            for d in self.analyzer.scopes[target]["tree"].decorator_list
        )

    def bind_executor(self, api: str, call: ast.Call, caller: str) -> int:
        qualified = self.analyzer.nodes[api]["qualified_name"]
        if qualified in COROUTINE_SCHEDULERS and call.args:
            coroutine = call.args[0]
            if isinstance(coroutine, ast.Call):
                self.task_submissions[
                    (caller, coroutine.lineno, coroutine.col_offset)
                ] = {
                    "kind": "coroutine-submission",
                    "api": qualified,
                    "argument": expression(coroutine),
                    "parameter": "coro",
                    "execution": "task",
                    "caller_id": caller,
                    "path": self.analyzer.scopes[caller]["path"],
                    "line": call.lineno,
                    "column": call.col_offset,
                }
        method = EXECUTOR_CALLBACKS.get(qualified)
        model = EXECUTION_CALLBACKS.get(qualified)
        if method is None and model is None:
            return 0
        bound = isinstance(call.func, ast.Attribute) and any(
            reason in INSTANCE_REASONS
            for _, reason in self.resolve(call.func.value, caller, call.lineno)
        )
        offset = 0 if bound or (model and model[2] == "thread") else 1
        parameter = "fn" if method else model[1]
        if model:
            offset += model[0]
        args = call.args[offset:]
        callback = (
            args[0]
            if args
            else next((k.value for k in call.keywords if k.arg == parameter), None)
        )
        if callback is None:
            return 0
        refs = self.resolve(callback, caller, call.lineno)
        targets = []
        bound_targets = set()
        callable_methods = set()
        for target, reason in refs:
            node = self.analyzer.nodes[target]
            if node["symbol_type"] == "class" and reason in INSTANCE_REASONS:
                if target in self.analyzer.scopes:
                    methods = self.analyzer._method(target, "__call__", set())
                    targets.extend(methods)
                    callable_methods.update(methods)
            elif (
                node["symbol_type"] in {"function", "lambda", "class"}
                or node["kind"] == "external"
            ):
                targets.append(target)
                if reason == "bound_method":
                    bound_targets.add(target)
        evidence = {
            "kind": "callback-argument",
            "api": qualified,
            "argument": expression(callback),
            "parameter": parameter,
            "execution": "executor" if method else model[2],
            "caller_id": caller,
            "caller": self.analyzer.nodes[caller]["qualified_name"],
            "path": self.analyzer.scopes[caller]["path"],
            "line": call.lineno,
            "column": call.col_offset,
        }
        if not targets:
            self.callbacks[(caller, call.lineno, call.col_offset, None)] = {
                "call": call,
                "callback": callback,
                "argument": expression(callback),
                "evidence": evidence,
            }
            return 0
        self.callbacks.pop((caller, call.lineno, call.col_offset, None), None)
        supplied = args[1:]
        keywords = (
            [k for k in call.keywords if args or k.arg != parameter]
            if method == "submit" or (model and model[2] != "executor")
            else []
        )
        if method == "map":
            supplied = [
                (
                    arg.elts[0]
                    if len(arg.elts) == 1
                    else ast.BoolOp(op=ast.Or(), values=list(arg.elts))
                )
                if isinstance(arg, (ast.List, ast.Tuple, ast.Set)) and arg.elts
                else ast.Name(id="<unknown iterable item>")
                for arg in supplied
            ]
        synthetic = ast.copy_location(
            ast.Call(func=callback, args=supplied, keywords=keywords), call
        )
        additions = 0
        for target in sorted(set(targets)):
            self.callbacks[(caller, call.lineno, call.col_offset, target)] = {
                "call": call,
                "callback": callback,
                "argument": expression(callback),
                "evidence": evidence | {"target_id": target},
            }
            additions += self.activate(target)
            scope = self.analyzer.scopes.get(target)
            if scope is None:
                continue
            if isinstance(scope["tree"], ast.ClassDef):
                for init in self.analyzer._method(target, "__init__", set()):
                    additions += self.activate(init)
                    additions += self.bind(init, synthetic, caller, bound=True)
            else:
                additions += self.bind(
                    target,
                    synthetic,
                    caller,
                    bound=self.instance_bound(target)
                    if target in callable_methods
                    else target in bound_targets
                    or self.is_bound(target, callback, caller),
                )
        return additions

    def is_bound(
        self, target: str, func: ast.AST, caller: str, reason: str | None = None
    ) -> bool:
        if reason == "bound_method":
            return True
        scope = self.analyzer.scopes[target]
        if not scope["class_id"] or not isinstance(func, ast.Attribute):
            return False
        decorators = {
            expression(d).split(".")[-1] for d in scope["tree"].decorator_list
        }
        if "staticmethod" in decorators:
            return False
        if "classmethod" in decorators:
            return True
        return any(
            reason in INSTANCE_REASONS
            for _, reason in self.resolve(
                func.value, caller, getattr(func, "lineno", 10**9)
            )
        )

    def bind(
        self,
        target: str,
        call: ast.Call,
        caller: str,
        bound: bool,
        closures: dict | None = None,
    ) -> int:
        tree = self.analyzer.scopes[target]["tree"]
        params = tree.args.posonlyargs + tree.args.args
        if bound:
            params = params[1:]
        supplied: list[tuple[str, ast.AST]] = []
        positional = []
        positional_complete = True
        for arg in call.args:
            if isinstance(arg, ast.Starred):
                if isinstance(arg.value, (ast.List, ast.Tuple)):
                    positional.extend(arg.value.elts)
                else:
                    positional_complete = False
                    break
            else:
                positional.append(arg)
        supplied.extend((param.arg, arg) for param, arg in zip(params, positional))
        allowed_keywords = {p.arg for p in tree.args.args + tree.args.kwonlyargs}
        for keyword in call.keywords:
            if keyword.arg in allowed_keywords:
                supplied.append((keyword.arg, keyword.value))
            elif keyword.arg is None and isinstance(keyword.value, ast.Dict):
                supplied.extend(
                    (key.value, value)
                    for key, value in zip(keyword.value.keys, keyword.value.values)
                    if isinstance(key, ast.Constant) and key.value in allowed_keywords
                )
        # Defaults are analyzed at their definition, not in the caller's namespace.
        given = {name for name, _ in supplied}
        defaults = (
            dict(
                zip(
                    [
                        p.arg
                        for p in (tree.args.posonlyargs + tree.args.args)[
                            -len(tree.args.defaults) :
                        ]
                    ],
                    tree.args.defaults,
                )
            )
            if tree.args.defaults
            else {}
        )
        defaults.update(
            {
                p.arg: value
                for p, value in zip(tree.args.kwonlyargs, tree.args.kw_defaults)
                if value is not None
            }
        )
        additions = 0
        if bound:
            all_params = tree.args.posonlyargs + tree.args.args
            if all_params:
                additions += self.add_arguments(
                    target,
                    all_params[0].arg,
                    self.method_receivers.get(target, []),
                    caller,
                    call.lineno,
                    call.col_offset,
                    expression(call.func),
                )
        invocation = {
            "target": target,
            "arguments": {},
            "dependencies": {},
            "constants": {},
            "closures": closures or {},
        }
        for name, arg in supplied:
            invocation["dependencies"][name] = self.parameter_dependencies(arg, caller)
            self.observed_arguments[(target, name)].add(
                (caller, call.lineno, call.col_offset, expression(arg))
            )
            refs = self.resolve(arg, caller, call.lineno)
            invocation["arguments"][name] = refs
            additions += self.add_arguments(
                target,
                name,
                refs,
                caller,
                call.lineno,
                call.col_offset,
                expression(arg),
            )
            if self.registry and (target, name) in self.registry.key_parameters:
                self.registry.mark_key_parameters(arg, caller)
                values = self.registry.literals(arg, caller)
                invocation["constants"][name] = values
                before = len(self.constants[(target, name)])
                self.constants[(target, name)].update(values)
                additions += len(self.constants[(target, name)]) - before
        if positional_complete and all(k.arg is not None for k in call.keywords):
            for name, default in defaults.items():
                if name not in given:
                    parent = self.analyzer.scopes[target]["parent"]
                    refs = self.resolve(default, parent, 10**9)
                    additions += self.add_arguments(
                        target,
                        name,
                        refs,
                        caller,
                        call.lineno,
                        call.col_offset,
                        expression(default),
                    )
                    invocation["arguments"][name] = refs
                    if self.registry and (target, name) in self.registry.key_parameters:
                        values = self.registry.literals(default, parent)
                        invocation["constants"][name] = values
                        before = len(self.constants[(target, name)])
                        self.constants[(target, name)].update(values)
                        additions += len(self.constants[(target, name)]) - before
        invocation_key = (target, caller, call.lineno, call.col_offset, bound)
        self.invocations[invocation_key] = invocation
        self.invocations_by_target[target][invocation_key] = invocation
        return additions

    def parameter_dependencies(self, expr, source: str, seen=None) -> list[str]:
        """Track direct forwarding and local aliases without guessing transforms."""
        seen = set() if seen is None else seen
        if not isinstance(expr, ast.Name) or expr.id in seen:
            return []
        scope = self.analyzer.scopes[source]
        if expr.id in scope["parameters"]:
            if any(kind == "value" for _, kind, _ in scope["bindings"][expr.id]):
                return []
            return [expr.id]
        aliases = [
            value
            for _, kind, value in scope["bindings"].get(expr.id, [])
            if kind == "value"
        ]
        dependencies = [
            self.parameter_dependencies(value, source, seen | {expr.id})
            for value in aliases
        ]
        if not dependencies or not all(dependencies):
            return []
        return sorted(
            {parameter for parameters in dependencies for parameter in parameters}
        )

    def edge_arguments(self, source, target, call) -> dict:
        if not isinstance(call, ast.Call):
            return {}
        records = [
            v
            for k, v in self.invocations_by_target.get(target, {}).items()
            if k[1:4] == (source, call.lineno, call.col_offset)
        ]
        return {
            name: {
                "value_ids": sorted(
                    {ref for v in records for ref, _ in v["arguments"].get(name, [])}
                ),
                "source_parameters": sorted(
                    {p for v in records for p in v["dependencies"].get(name, [])}
                ),
            }
            for name in {name for v in records for name in v["arguments"]}
        }

    def bind_decorators(self) -> int:
        additions = 0
        for parent, decorator, tree in self.decorator_sites:
            closures = {}
            if isinstance(decorator, ast.Call):
                for target, _ in self.resolve(decorator.func, parent, tree.lineno):
                    if target in self.returns:
                        additions += self.bind(target, decorator, parent, bound=False)
                        closures[target] = self.invocations[
                            (
                                target,
                                parent,
                                decorator.lineno,
                                decorator.col_offset,
                                False,
                            )
                        ]
            call = ast.copy_location(
                ast.Call(func=decorator, args=[ast.Name(id=tree.name)], keywords=[]),
                tree,
            )
            for target, _ in self.resolve(decorator, parent, tree.lineno):
                if target in self.returns:
                    additions += self.bind(
                        target, call, parent, bound=False, closures=closures
                    )
        return additions

    def add_arguments(
        self, target, name, refs, caller, line, column=0, value_expression=""
    ) -> int:
        key = (target, name)
        before = len(self.arguments[key])
        for ref, reason in refs:
            fact = (ref, argument_reason(ref, reason, self.analyzer.nodes))
            if (
                fact not in self.arguments[key]
                and len(self.arguments[key]) >= self.MAX_FACTS
            ):
                self.saturated.add(key)
                continue
            self.arguments[key].add(fact)
            self.argument_sources[(target, name, ref)].add(
                (caller, line, column, value_expression)
            )
            self.sources_by_callee[target][(name, ref)] = self.argument_sources[
                (target, name, ref)
            ]
        return len(self.arguments[key]) - before

    def return_values(self, target: str, seen: set) -> list:
        if target in self.return_cache:
            return self.return_cache[target]
        if target in self.return_active:
            return list(self.previous_returns[target])
        self.return_active.add(target)
        result = []
        for value in self.returns.get(target, []):
            for ref, reason in self.analyzer._resolve(
                value, target, getattr(value, "lineno", 10**9), seen
            ):
                if (
                    self.analyzer.nodes[ref]["symbol_type"] == "class"
                    and reason in INSTANCE_REASONS
                ):
                    result.append((ref, "argument_instance"))
                elif reason in {"registry", "bound_method"} or reason.startswith(
                    ("super_instance:", "super_class:")
                ):
                    result.append((ref, reason))
                else:
                    result.append((ref, "return_flow"))
        self.return_active.remove(target)
        known = self.previous_returns[target]
        before = len(known)
        new = sorted(set(result) - known)
        if len(known) + len(new) > self.MAX_FACTS:
            self.saturated_returns.add(target)
        known.update(new[: max(0, self.MAX_FACTS - len(known))])
        self.return_additions += len(known) - before
        self.return_cache[target] = sorted(known)
        return self.return_cache[target]

    def external_returns(self, qualified: str, seen: set | None = None) -> list:
        if qualified in self.library_results:
            return self.library_results[qualified]
        seen = seen or set()
        if qualified in seen or len(seen) > 8:
            return []
        seen = seen | {qualified}
        a = self.analyzer
        result = []
        record = self.docs.definition(qualified)
        if record and isinstance(record["tree"], ast.ClassDef):
            target = a._external(qualified)
            a.nodes[target]["symbol_type"] = "class"
            result.append((target, "external_instance"))
        elif record and isinstance(
            record["tree"], (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            tree = record["tree"]
            if tree.returns:
                annotation = tree.returns
                role = "external_instance"
                if isinstance(annotation, ast.Subscript) and expression(
                    annotation.value
                ).split(".")[-1] in {"Type", "type"}:
                    annotation, role = annotation.slice, "external_class"
                for name in self.docs.type_names(annotation, record):
                    if name.endswith(".Self"):
                        name = qualified.rpartition(".")[0]
                    if name.endswith((".Callable", ".Any")):
                        continue
                    target = a._external(name)
                    a.nodes[target]["symbol_type"] = "class"
                    result.append((target, role))
            if not result:
                for stmt in owned_walk(tree):
                    if not isinstance(stmt, ast.Return) or not isinstance(
                        stmt.value, ast.Call
                    ):
                        continue
                    for name in self.docs.type_names(stmt.value.func, record):
                        result.extend(self.external_returns(name, seen))
        if not result and qualified in RETURNS:
            target = a._external(RETURNS[qualified])
            a.nodes[target]["symbol_type"] = "class"
            result.append((target, "external_instance"))
        if qualified in CLASSES:
            target = a._external(qualified)
            a.nodes[target]["symbol_type"] = "class"
            result = [(target, "external_instance")]
        self.library_results[qualified] = list(set(result))
        return self.library_results[qualified]

    def external_method(self, qualified: str) -> str | None:
        record = self.docs.definition(qualified)
        if record:
            return record["qualified"]
        owner, _, method = qualified.rpartition(".")
        if method in CLASSES.get(owner, {}):
            target = self.analyzer._external(qualified)
            self.analyzer.nodes[target].update(
                {
                    "doc": CLASSES[owner][method],
                    "doc_source": "stdlib-contract",
                    "signature": qualified
                    + CLASSES[owner][method].split(" — ")[0][len(method) :],
                }
            )
            return qualified
        # A known external receiver type is already sufficient to name an API
        # boundary. Missing installed declarations means missing documentation,
        # not an unknown receiver. A present class with a missing method is kept
        # unresolved, so this does not invent members of inspected classes.
        if owner not in CLASSES and self.docs.definition(owner) is None:
            target = self.analyzer._external(qualified)
            self.analyzer.nodes[target]["declaration_available"] = False
            return qualified
        return None

    def summary(self) -> dict:
        return {
            "argument_rounds": self.rounds,
            "argument_converged": self.converged,
            "argument_bindings": sum(len(v) for v in self.arguments.values()),
            "saturated_parameters": len(self.saturated),
            "saturated_returns": len(self.saturated_returns),
            "iterations": list(self.iterations),
        }

    def callsite_bindings(self, source: str, call: ast.Call) -> list[dict]:
        """Resolve parameter calls separately for each observed invocation.

        Keep unknown inputs explicit: an empty target set is not proof that a
        wrapper cannot execute another callback. No inferred global facts or
        caches are changed by this view of a single invocation.
        """
        result = []
        for key, invocation in self.invocations_by_target.get(source, {}).items():
            arguments = {
                name: [
                    (ref, argument_reason(ref, reason, self.analyzer.nodes))
                    for ref, reason in refs
                ]
                for name, refs in invocation["arguments"].items()
            }
            self.lookup_context = (source, arguments)
            try:
                refs = self.analyzer._resolve(call.func, source, call.lineno)
            finally:
                self.lookup_context = None
            targets = set()
            for ref, reason in refs:
                if (
                    reason in INSTANCE_REASONS
                    and self.analyzer.nodes[ref]["symbol_type"] == "class"
                ):
                    targets.update(self.analyzer._method(ref, "__call__", set()))
                else:
                    targets.add(ref)
            result.append(
                {
                    "caller_id": key[1],
                    "line": key[2],
                    "column": key[3],
                    "target_ids": sorted(targets),
                    "unknown": not bool(targets),
                }
            )
        return result

    def evidence(self, source: str, target: str, reason: str, call=None) -> list[dict]:
        result = []
        if reason == "unresolved" and isinstance(call, ast.Call):
            parameter = expression(call.func).split(".")[0]
            for caller, line, column, argument in sorted(
                self.observed_arguments.get((source, parameter), [])
            ):
                result.append(
                    {
                        "kind": "observed-argument",
                        "parameter": parameter,
                        "argument": argument,
                        "caller_id": caller,
                        "caller": self.analyzer.nodes[caller]["qualified_name"],
                        "path": self.analyzer.scopes[caller]["path"],
                        "line": line,
                        "column": column,
                        "candidate_known": False,
                    }
                )
        if self.registry and reason in {
            "registry",
            "registry_instance",
            "bound_method",
        }:
            result.extend(self.registry.evidence(target))
        if reason in {"argument", "argument_instance", "bound_method"}:
            for (parameter, value), origins in self.sources_by_callee.get(
                source, {}
            ).items():
                value_node = self.analyzer.nodes[value]
                relevant = value == target
                if (
                    value_node["symbol_type"] == "class"
                    and value in self.analyzer.scopes
                ):
                    classes = {value}
                    if self.active_scopes is None:
                        classes |= self.analyzer._descendants(value)
                    relevant = any(
                        target
                        in self.analyzer._method(
                            c, self.analyzer.nodes[target]["name"], set()
                        )
                        for c in classes
                    )
                if not relevant:
                    continue
                if call is not None and isinstance(
                    call.func, (ast.Name, ast.Attribute)
                ):
                    receiver = expression(call.func).split(".")[0]
                    if (
                        receiver in self.analyzer.scopes[source]["parameters"]
                        and receiver != parameter
                    ):
                        continue
                for caller, line, column, arg_expression in sorted(origins):
                    result.append(
                        {
                            "kind": "argument-flow",
                            "parameter": parameter,
                            "value": self.analyzer.nodes[value]["qualified_name"],
                            "value_id": value,
                            "target_id": target,
                            "caller_id": caller,
                            "caller": self.analyzer.nodes[caller]["qualified_name"],
                            "argument": arg_expression,
                            "path": self.analyzer.scopes[caller]["path"],
                            "line": line,
                            "column": column,
                        }
                    )
        node = self.analyzer.nodes[target]
        if reason == "external_return":
            definition = self.docs.definition(node["qualified_name"])
            if definition:
                result.append(
                    {
                        "kind": "external-declaration",
                        "path": str(definition["path"]),
                        "line": definition["tree"].lineno,
                    }
                )
            else:
                result.append(
                    {
                        "kind": "external-type-contract"
                        if node.get("declaration_available") is False
                        else "stdlib-contract",
                        "symbol": node["qualified_name"],
                    }
                )
        return result
