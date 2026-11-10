"""Definitions and observable reads/writes; never evaluates configuration."""

from __future__ import annotations

import ast
import fnmatch
import hashlib


def build_references(analyzer) -> list[dict]:
    records = []
    canonical_cache = {}
    for sid, scope in analyzer.scopes.items():
        tree = scope["tree"]
        owner = analyzer.nodes[sid]
        if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            records.append(
                {
                    "expression": tree.name,
                    "canonical": [owner["qualified_name"]],
                    "kind": "definition",
                    "symbol_id": sid,
                    "path": scope["path"],
                    "line": tree.lineno,
                    "column": tree.col_offset,
                    "initializer": "",
                    "binding": "lexical",
                }
            )
        from callmap.analyzer import owned_walk

        assignments = {}
        declared = set()
        for node in owned_walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                ):
                    for part in ast.walk(target):
                        assignments[id(part)] = (
                            ast.unparse(node.value) if node.value else ""
                        )
        for node in owned_walk(tree):
            if not isinstance(node, (ast.Name, ast.Attribute)):
                continue
            expression = ast.unparse(node)
            canonical = []
            if isinstance(node, ast.Name):
                lexical = sid
                while (
                    expression not in analyzer.scopes[lexical]["bindings"]
                    and analyzer.scopes[lexical]["parent"]
                ):
                    lexical = analyzer.scopes[lexical]["parent"]
                    if isinstance(
                        analyzer.scopes[lexical]["tree"], ast.ClassDef
                    ) and isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        lexical = analyzer.scopes[lexical]["parent"] or lexical
                canonical = [
                    analyzer.nodes[lexical]["qualified_name"] + "." + expression
                ]
            else:
                key = (sid, node.lineno, ast.unparse(node.value))
                if key not in canonical_cache:
                    canonical_cache[key] = analyzer.flow.resolve(
                        node.value, sid, node.lineno
                    )
                canonical = sorted(
                    {
                        analyzer.nodes[ref]["qualified_name"] + "." + node.attr
                        for ref, _ in canonical_cache[key]
                        if analyzer.nodes[ref]["symbol_type"] in {"class", "module"}
                    }
                )
            write = isinstance(node.ctx, (ast.Store, ast.Del))
            declaration = (
                write
                and expression not in declared
                and (
                    isinstance(tree, (ast.Module, ast.ClassDef))
                    or (
                        getattr(tree, "name", "") == "__init__"
                        and expression.startswith("self.")
                    )
                )
            )
            if write:
                declared.add(expression)
            records.append(
                {
                    "expression": expression,
                    "canonical": canonical,
                    "kind": "definition"
                    if declaration
                    else "write"
                    if write
                    else "read",
                    "access": "write" if write else "read",
                    "symbol_id": sid,
                    "path": scope["path"],
                    "line": node.lineno,
                    "column": node.col_offset,
                    "initializer": assignments.get(id(node), ""),
                    "binding": "type_candidate"
                    if isinstance(node, ast.Attribute)
                    else "lexical",
                }
            )
    for record in records:
        record["id"] = (
            "r_"
            + hashlib.sha256(
                repr(
                    (
                        record["path"],
                        record["line"],
                        record["column"],
                        record["kind"],
                        record["expression"],
                    )
                ).encode()
            ).hexdigest()[:24]
        )
    return sorted(records, key=lambda r: (r["path"], r["line"], r["column"], r["kind"]))


def search_references(records, query, path=None, kind=None):
    return [
        r
        for r in records
        if (
            query.lower() in r["expression"].lower()
            or any(query.lower() in c.lower() for c in r["canonical"])
        )
        and (path is None or fnmatch.fnmatch(r["path"], path))
        and (kind is None or r["kind"] == kind or r.get("access") == kind)
    ]
