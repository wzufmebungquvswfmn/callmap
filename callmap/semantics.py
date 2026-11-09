"""Observable callsite facts and explanations of static resolution."""

from __future__ import annotations

import ast
import json
from collections import defaultdict


def text(node: ast.AST) -> str:
    return ast.unparse(node)


class CallsiteMetadata:
    def __init__(self, analyzer):
        self.a = analyzer
        self.parents = {}

    def literal(self, node, source, seen=None) -> dict:
        seen = set() if seen is None else seen
        if isinstance(node, ast.Name) and (source, node.id) not in seen:
            scope = self.a.scopes[source]
            bindings = scope["bindings"].get(node.id, [])
            # Multiple assignments and caller parameters are not constants.
            if node.id not in scope["parameters"] and len(bindings) == 1:
                _, kind, value = bindings[0]
                if kind == "value":
                    return self.literal(value, source, seen | {(source, node.id)})
            if not bindings and scope["parent"]:
                return self.literal(node, scope["parent"], seen | {(source, node.id)})
        try:
            if node is None or sum(1 for _ in ast.walk(node)) > 64:
                return {"known": False}
            value = ast.literal_eval(node)
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
            if len(encoded) <= 512:
                return {"known": True, "value": value}
        except (ValueError, TypeError, OverflowError, RecursionError):
            pass
        return {"known": False}

    def conditions(self, source, call) -> list[dict]:
        if source not in self.parents:
            self.parents[source] = {
                id(child): parent
                for parent in ast.walk(self.a.scopes[source]["tree"])
                for child in ast.iter_child_nodes(parent)
            }
        parents = self.parents[source]
        child = call
        result = []
        while id(child) in parents:
            parent = parents[id(child)]
            condition = None
            if isinstance(parent, (ast.If, ast.IfExp)):
                body = parent.body if isinstance(parent.body, list) else [parent.body]
                otherwise = (
                    parent.orelse
                    if isinstance(parent.orelse, list)
                    else [parent.orelse]
                )
                if child in body or child in otherwise:
                    condition = {
                        "kind": "branch",
                        "expression": text(parent.test),
                        "truthy": child in body,
                    }
            elif isinstance(parent, ast.BoolOp) and child in parent.values[1:]:
                previous = parent.values[: parent.values.index(child)]
                condition = {
                    "kind": "short_circuit",
                    "expression": text(ast.BoolOp(op=parent.op, values=previous))
                    if len(previous) > 1
                    else text(previous[0]),
                    "truthy": isinstance(parent.op, ast.And),
                }
            elif isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
                if child in parent.body:
                    condition = {
                        "kind": "loop",
                        "expression": text(parent.test)
                        if isinstance(parent, ast.While)
                        else text(parent.iter),
                        "requirement": "test_truthy"
                        if isinstance(parent, ast.While)
                        else "iteration_available",
                    }
                elif child in parent.orelse:
                    condition = {"kind": "loop_else", "requirement": "no_break"}
            elif isinstance(parent, ast.ExceptHandler) and child in parent.body:
                condition = {
                    "kind": "exception_handler",
                    "expression": text(parent.type) if parent.type else "BaseException",
                }
            elif isinstance(parent, (ast.Try, ast.TryStar)):
                if child in parent.orelse:
                    condition = {"kind": "try_else", "requirement": "no_exception"}
                elif child in parent.finalbody:
                    condition = {"kind": "finally", "requirement": "try_entered"}
            elif isinstance(parent, ast.match_case) and child in parent.body:
                condition = {"kind": "match_case", "expression": text(parent.pattern)}
                if parent.guard:
                    condition["guard"] = text(parent.guard)
            if condition:
                result.append(
                    condition | {"line": getattr(parent, "lineno", call.lineno)}
                )
            child = parent
        return list(reversed(result))

    def describe(self, source, target, call, reason, synthetic) -> dict:
        if not isinstance(call, ast.Call):
            return {
                "path": self.a.nodes[source]["path"],
                "arguments": [],
                "conditions": [],
                "execution": {"mode": "constructor", "awaited": False},
            }
        conditions = self.conditions(source, call)
        parent = self.parents[source].get(id(call))
        awaited = isinstance(parent, ast.Await) and parent.value is call
        mode = "await_result" if awaited else "call"
        parameters = []
        scope = self.a.scopes.get(target)
        if scope and not synthetic and hasattr(scope["tree"], "args"):
            parameters = scope["tree"].args.posonlyargs + scope["tree"].args.args
            if self.a.flow.is_bound(target, call.func, source, reason):
                parameters = parameters[1:]
        arguments = []
        position_known = True
        for index, arg in enumerate(call.args):
            item = {
                "kind": "starred" if isinstance(arg, ast.Starred) else "positional",
                "position": index,
                "expression": text(arg),
            }
            if isinstance(arg, ast.Starred):
                position_known = False
            elif position_known and index < len(parameters):
                item["parameter"] = parameters[index].arg
            item.update(self.literal(arg, source))
            arguments.append(item)
        for keyword in call.keywords:
            arguments.append(
                {
                    "kind": "keyword" if keyword.arg else "keyword_unpack",
                    "parameter": keyword.arg,
                    "expression": text(keyword.value),
                }
                | self.literal(keyword.value, source)
            )
        if (
            scope
            and not synthetic
            and hasattr(scope["tree"], "args")
            and position_known
            and all(k.arg for k in call.keywords)
        ):
            tree = scope["tree"]
            all_params = tree.args.posonlyargs + tree.args.args
            defaults = (
                dict(
                    zip(
                        [p.arg for p in all_params[-len(tree.args.defaults) :]],
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
            supplied = {item.get("parameter") for item in arguments}
            for name, value in defaults.items():
                if name not in supplied:
                    arguments.append(
                        {
                            "kind": "default",
                            "parameter": name,
                            "expression": text(value),
                        }
                        | self.literal(value, scope["parent"])
                    )
        if self.a.flow.registry:
            for _, owner, _, value, line in self.a.flow.registry.writes:
                if owner != target:
                    continue
                refs = self.a.flow.resolve(value, target, line)
                if any(
                    self.a.nodes[ref]["symbol_type"] in {"function", "lambda"}
                    for ref, _ in refs
                ):
                    mode = "registry_write_call"
        return {
            "path": self.a.nodes[source]["path"],
            "arguments": arguments,
            "conditions": conditions,
            "execution": {"mode": mode, "awaited": awaited},
        }


BASIS = {
    "import": "direct_import",
    "lexical": "lexical_binding",
    "builtin": "builtin",
    "constructor": "constructor",
    "instance": "instance_type",
    "external_instance": "instance_type",
    "bound_method": "instance_type",
    "annotation": "type_annotation",
    "class_annotation": "type_annotation",
    "argument": "argument_flow",
    "argument_instance": "argument_flow",
    "return_flow": "return_flow",
    "external_return": "external_return_type",
    "registry": "registry",
    "registry_instance": "registry",
    "manual": "manual_annotation",
    "inference": "semantic_inference",
    "unresolved": "unresolved",
    "executor-callback": "stdlib_contract",
    "execution-callback": "stdlib_contract",
}


def describe_edges(edges: list[dict], nodes: dict) -> None:
    """Explain candidates without claiming any runtime invocation is guaranteed."""
    groups = defaultdict(set)
    for edge in edges:
        if edge["status"] != "unresolved":
            groups[
                (edge["source"], edge["line"], edge["column"], edge["expression"])
            ].add(edge["target"])
    for edge in edges:
        target = nodes[edge["target"]]
        count = len(
            groups[(edge["source"], edge["line"], edge["column"], edge["expression"])]
        )
        reason = edge["reason"]
        basis = {BASIS.get(reason, "static_binding")}
        conditions = ["调用点实际执行时成立"]
        unknowns = ["运行时是否进入此调用点"]
        for proof in edge.get("evidence", []):
            if proof["kind"] == "argument-flow":
                basis.add("argument_flow")
            elif proof["kind"] == "registry-write":
                basis.add("registry")
            elif proof["kind"] == "stdlib-contract":
                basis.add("stdlib_contract")
        if reason in {"annotation", "argument_instance", "bound_method", "instance"}:
            conditions.append("接收对象需分派到 " + target["qualified_name"])
            unknowns.append("实际接收对象及运行时方法重写")
        if "registry" in basis:
            conditions.append("对应注册项已加载且查询键匹配")
            unknowns.append("运行时注册项、配置键及插件加载")
        if "argument_flow" in basis:
            conditions.append("实参需绑定到此目标；来源见参数传播证据")
            unknowns.append("未观察到的调用者及动态实参")
        if reason == "manual":
            conditions.append("人工标注有效且与运行时配置一致")
            unknowns.append("人工标注目标是否符合当前运行时分派")
        if target["kind"] == "external":
            unknowns.append("外部接口实现及运行时重绑定")
        if edge["status"] == "unresolved":
            unknowns.append("目标函数、接收对象类型或参数来源")
            summary = "识别到调用表达式，尚未定位目标。"
        elif count > 1:
            summary = f"调用点已识别，有 {count} 个静态候选目标；运行时选择尚未确认。"
        elif edge["status"] == "manual":
            summary = "目标由人工标注补充，尚未验证运行时分派。"
        elif edge["status"] == "candidate":
            summary = "根据静态证据得到候选目标，成立条件仍需确认。"
        else:
            summary = "静态定位到目标；resolved 表示解析结果，不保证运行时调用。"
        callsite = edge.get("callsite", {})
        execution = dict(callsite.get("execution", {"mode": "call", "awaited": False}))
        for proof in edge.get("evidence", []):
            if proof["kind"] == "callback-argument":
                execution["mode"] = {
                    "thread": "thread_submission",
                    "executor": "executor_submission",
                    "context": "context_execution",
                }.get(proof.get("execution"), "callback_execution")
                execution["wrapper"] = proof["api"]
            elif proof["kind"] == "coroutine-submission":
                execution["mode"] = "task_submission"
                execution["wrapper"] = proof["api"]
        if callsite:
            edge["callsite"] = callsite | {"execution": execution}
        edge["resolution"] = {
            "basis": sorted(basis),
            "candidate_count": count,
            "conditions": conditions,
            "unknowns": unknowns,
            "runtime_guaranteed": False,
            "summary": summary,
        }
