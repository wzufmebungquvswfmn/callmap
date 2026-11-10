"""Bounded guarded return propagation for explanations, not runtime execution."""

from __future__ import annotations

import ast
import copy
from collections import defaultdict


class Substitute(ast.NodeTransformer):
    def __init__(self, values):
        self.values = values

    def visit_Name(self, node):
        return copy.deepcopy(self.values.get(node.id, node))


class GuardedReturns:
    MAX_DEPTH = 8
    MAX_FACTS = 32

    def __init__(self, analyzer, references):
        self.a = analyzer
        self.references = references
        self.memo = {}
        self.limited = set()
        self.truncated = False
        self.definitions = defaultdict(list)
        self.names = defaultdict(list)
        for reference in references:
            if reference["kind"] in {"definition", "write"}:
                self.names[reference["expression"].split(".")[-1]].append(reference)
                for name in reference["canonical"]:
                    self.definitions[name].append(reference)

    def arguments(self, target, call, source):
        tree = self.a.scopes[target]["tree"]
        params = tree.args.posonlyargs + tree.args.args
        if self.a.flow.is_bound(target, call.func, source):
            params = params[1:]
        values = {}
        for param, arg in zip(params, call.args):
            if isinstance(arg, ast.Starred):
                break
            values[param.arg] = arg
        values.update({k.arg: k.value for k in call.keywords if k.arg})
        for param, default in zip(
            (tree.args.posonlyargs + tree.args.args)[-len(tree.args.defaults) :],
            tree.args.defaults,
        ):
            values.setdefault(param.arg, default)
        for param, default in zip(tree.args.kwonlyargs, tree.args.kw_defaults):
            if default is not None:
                values.setdefault(param.arg, default)
        return values

    def origin(self, kind, source, node):
        return {
            "kind": kind,
            "symbol_id": source,
            "symbol": self.a.nodes[source]["qualified_name"],
            "path": self.a.scopes[source]["path"],
            "line": getattr(node, "lineno", 1),
            "expression": ast.unparse(node),
        }

    def facts(self, expr, source, line, seen=()):
        if source not in self.a.scopes or expr is None:
            return []
        key = (source, line, ast.unparse(expr))
        if key in seen or len(seen) >= self.MAX_DEPTH:
            self.truncated = True
            return []
        if not seen and key in self.memo:
            self.truncated |= key in self.limited
            return self.memo[key]
        result = []
        scope = self.a.scopes[source]
        if isinstance(expr, ast.Name):
            bindings = scope["bindings"].get(expr.id)
            if bindings is None and scope["parent"]:
                result = self.facts(expr, scope["parent"], 10**9, seen + (key,))
            for binding_line, kind, value in bindings or []:
                if kind == "value" and binding_line <= line:
                    for fact in self.facts(
                        value, source, binding_line - 1, seen + (key,)
                    ):
                        result.append(
                            fact
                            | {
                                "origins": [self.origin("assignment", source, value)]
                                + fact["origins"]
                            }
                        )
            if expr.id in scope["parameters"] and expr.id not in {"self", "cls"}:
                observed = sorted(
                    self.a.flow.observed_arguments.get((source, expr.id), [])
                )
                if len(observed) > 8:
                    self.truncated = True
                for caller, at, _, expression in observed[:8]:
                    try:
                        arg = ast.parse(expression, mode="eval").body
                    except SyntaxError:
                        continue
                    result.extend(self.facts(arg, caller, at, seen + (key,)))
        elif isinstance(expr, ast.Call):
            for target, _ in self.a.flow.resolve(expr.func, source, line):
                target_scope = self.a.scopes.get(target)
                if target_scope is None:
                    continue
                if isinstance(target_scope["tree"], ast.ClassDef):
                    result.append(
                        {
                            "value_id": target,
                            "conditions": [],
                            "origins": [self.origin("construction", source, expr)],
                        }
                    )
                elif isinstance(
                    target_scope["tree"], (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    from callmap.analyzer import owned_walk

                    values = self.arguments(target, expr, source)
                    for returned in owned_walk(target_scope["tree"]):
                        if (
                            not isinstance(returned, ast.Return)
                            or returned.value is None
                        ):
                            continue
                        conditions = self.a.callsite_metadata.conditions(
                            target, returned
                        )
                        for fact in self.facts(
                            returned.value, target, returned.lineno, seen + (key,)
                        ):
                            mapped = []
                            for condition in conditions + fact["conditions"]:
                                item = condition.copy()
                                for field in ("expression", "guard"):
                                    if field in item:
                                        try:
                                            tree = ast.parse(item[field], mode="eval")
                                            item[field] = ast.unparse(
                                                Substitute(values).visit(tree).body
                                            )
                                        except SyntaxError:
                                            pass
                                item["source_id"] = source
                                mapped.append(item)
                            result.append(
                                fact
                                | {
                                    "conditions": mapped,
                                    "origins": [
                                        self.origin("factory_return", target, returned)
                                    ]
                                    + fact["origins"],
                                }
                            )
        if len(result) > self.MAX_FACTS:
            self.truncated = True
        result = result[: self.MAX_FACTS]
        if not seen:
            self.memo[key] = result
            if self.truncated:
                self.limited.add(key)
        return result

    def enrich(self, edges):
        calls = {
            (sid, call.lineno, call.col_offset): call
            for sid, call in self.a.flow.call_sites
        }
        for edge in edges:
            call = calls.get((edge["source"], edge["line"], edge["column"]))
            if (
                call is None
                or not isinstance(call.func, ast.Attribute)
                or edge["synthetic"]
            ):
                continue
            self.truncated = False
            paths = []
            for fact in self.facts(call.func.value, edge["source"], edge["line"]):
                if edge["target"] not in self.a._method(
                    fact["value_id"], call.func.attr, set()
                ):
                    continue
                if not any(o["kind"] == "factory_return" for o in fact["origins"]):
                    continue
                paths.append(
                    {
                        "conditions": edge["callsite"]["conditions"]
                        + fact["conditions"],
                        "origins": fact["origins"],
                    }
                )
            if paths:
                edge["callsite"]["guard_paths"] = paths
                edge["resolution"]["provenance"] = paths
                edge["resolution"]["basis"].append("factory_return")
                edge["resolution"]["source_summary"] = (
                    f"{ast.unparse(call.func.value)} 来自工厂返回值；"
                    "目标需满足对应返回路径条件，实际配置取值尚未确认。"
                )
                dependencies = []
                for path in paths:
                    for condition in path["conditions"]:
                        expression = condition.get("expression", "")
                        try:
                            tree = ast.parse(expression, mode="eval")
                        except SyntaxError:
                            continue
                        for node in ast.walk(tree):
                            if not isinstance(node, ast.Attribute):
                                continue
                            types = self.a.flow.resolve(
                                node.value, edge["source"], edge["line"]
                            )
                            canonicals = {
                                self.a.nodes[ref]["qualified_name"] + "." + node.attr
                                for ref, _ in types
                                if self.a.nodes[ref]["symbol_type"]
                                in {"class", "module"}
                            }
                            matches = (
                                list(
                                    {
                                        r["id"]: r
                                        for canonical in canonicals
                                        for r in self.definitions[canonical]
                                    }.values()
                                )
                                if canonicals
                                else self.names[node.attr]
                            )
                            dependencies.append(
                                {
                                    "expression": ast.unparse(node),
                                    "known": False,
                                    "binding": "type_candidates"
                                    if canonicals
                                    else "name_candidates",
                                    "query": sorted(canonicals)[0]
                                    if canonicals
                                    else node.attr,
                                    "sources": matches[:8],
                                    "source_count": len(matches),
                                }
                            )
                edge["resolution"]["configuration_sources"] = dependencies
            if self.truncated:
                edge["resolution"]["provenance_truncated"] = True
