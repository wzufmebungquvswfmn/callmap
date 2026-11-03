"""Read installed library documentation from source, without importing libraries."""

from __future__ import annotations

import ast
import sys
import tokenize
from pathlib import Path


class LibraryDocs:
    def __init__(self, roots: list[Path]):
        self.roots = roots + [Path(p) for p in sys.path if p]
        self.cache: dict[Path, ast.Module | None] = {}
        self.definitions: dict[str, dict | None] = {}

    def definition(self, qualified: str, seen: set | None = None) -> dict | None:
        """Find a declaration and its lexical imports, without executing code."""
        if qualified in self.definitions:
            return self.definitions[qualified]
        seen = seen or set()
        if qualified in seen or len(seen) > 12:
            return None
        seen = seen | {qualified}
        parts = qualified.split(".")
        for split in range(len(parts), 0, -1):
            module = ".".join(parts[:split])
            for root in self.roots:
                stem = root.joinpath(*parts[:split])
                for path in (
                    stem.with_suffix(".pyi"),
                    stem.with_suffix(".py"),
                    stem / "__init__.pyi",
                    stem / "__init__.py",
                ):
                    tree = self._tree(path)
                    if tree is None:
                        continue
                    obj = tree
                    for name in parts[split:]:
                        obj = next(
                            (
                                n
                                for n in obj.body
                                if isinstance(
                                    n,
                                    (
                                        ast.ClassDef,
                                        ast.FunctionDef,
                                        ast.AsyncFunctionDef,
                                    ),
                                )
                                and n.name == name
                            ),
                            None,
                        )
                        if obj is None:
                            break
                    if obj is not None and parts[split:]:
                        record = {
                            "tree": obj,
                            "module_tree": tree,
                            "module": module,
                            "qualified": qualified,
                            "path": path,
                        }
                        self.definitions[qualified] = record
                        return record
                    # Follow static re-exports, including stdlib C aliases.
                    if parts[split:]:
                        target_name = parts[split]
                        suffix = ".".join(parts[split + 1 :])
                        for stmt in tree.body:
                            if isinstance(stmt, ast.ImportFrom):
                                for alias in stmt.names:
                                    if (alias.asname or alias.name) == target_name:
                                        origin = self.import_origin(stmt, module, path)
                                        redirected = origin + "." + alias.name
                                        if suffix:
                                            redirected += "." + suffix
                                        found = self.definition(redirected, seen)
                                        if found:
                                            self.definitions[qualified] = found
                                            return found
                    # Inherited methods declared in a base class.
                    if split < len(parts) - 1:
                        owner = self.definition(".".join(parts[:-1]), seen)
                        if owner and isinstance(owner["tree"], ast.ClassDef):
                            for base in owner["tree"].bases:
                                for name in self.type_names(base, owner):
                                    found = self.definition(
                                        name + "." + parts[-1], seen
                                    )
                                    if found:
                                        self.definitions[qualified] = found
                                        return found
        self.definitions[qualified] = None
        return None

    @staticmethod
    def import_origin(stmt: ast.ImportFrom, module: str, path: Path) -> str:
        package = module.split(".")
        if not path.name.startswith("__init__."):
            package = package[:-1]
        prefix = package[: len(package) - stmt.level + 1] if stmt.level else []
        return ".".join(prefix + ([stmt.module] if stmt.module else []))

    def type_names(self, node: ast.AST, definition: dict) -> list[str]:
        """Resolve declaration-local type names and import aliases."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                return self.type_names(
                    ast.parse(node.value, mode="eval").body, definition
                )
            except SyntaxError:
                return []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.type_names(node.left, definition) + self.type_names(
                node.right, definition
            )
        if isinstance(node, ast.Subscript):
            outer = ast.unparse(node.value).split(".")[-1]
            if outer in {"Optional", "Union", "Annotated"}:
                parts = (
                    node.slice.elts
                    if isinstance(node.slice, ast.Tuple)
                    else [node.slice]
                )
                if outer == "Annotated":
                    parts = parts[:1]
                return [n for p in parts for n in self.type_names(p, definition)]
            node = node.value
        if not isinstance(node, (ast.Name, ast.Attribute)):
            return []
        name = ast.unparse(node)
        if name in {"Any", "None", "NoReturn", "Never"}:
            return []
        if name in {
            "str",
            "bytes",
            "bool",
            "int",
            "float",
            "dict",
            "list",
            "tuple",
            "set",
            "object",
        }:
            return ["builtins." + name]
        root, *tail = name.split(".")
        for stmt in definition["module_tree"].body:
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    if (alias.asname or alias.name) == root:
                        origin = self.import_origin(
                            stmt, definition["module"], definition["path"]
                        )
                        return [".".join([origin, alias.name] + tail)]
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if (alias.asname or alias.name.split(".")[0]) == root:
                        return [".".join([alias.name if alias.asname else root] + tail)]
        return [definition["module"] + "." + name]

    def lookup(self, qualified: str, seen: set | None = None) -> dict:
        seen = seen or set()
        if qualified in seen or len(seen) > 8:
            return {}
        seen = seen | {qualified}
        parts = qualified.split(".")
        for split in range(len(parts), 0, -1):
            for root in self.roots:
                module_path = root.joinpath(*parts[:split])
                candidates = [
                    module_path.with_suffix(".pyi"),
                    module_path.with_suffix(".py"),
                    module_path / "__init__.pyi",
                    module_path / "__init__.py",
                ]
                for path in candidates:
                    tree = self._tree(path)
                    if tree is None:
                        continue
                    body = tree.body
                    obj = tree
                    rest = parts[split:]
                    for name in rest:
                        obj = next(
                            (
                                n
                                for n in body
                                if isinstance(
                                    n,
                                    (
                                        ast.FunctionDef,
                                        ast.AsyncFunctionDef,
                                        ast.ClassDef,
                                    ),
                                )
                                and n.name == name
                            ),
                            None,
                        )
                        if obj is None:
                            break
                        body = obj.body
                    if obj is not None and rest:
                        doc = ast.get_docstring(obj) or ""
                        signature = qualified
                        if isinstance(obj, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            signature += "(" + ast.unparse(obj.args) + ")"
                            if obj.returns:
                                signature += " -> " + ast.unparse(obj.returns)
                        return {
                            "doc": doc,
                            "signature": signature,
                            "doc_source": str(path),
                            "library_path": str(path),
                        }
                    if len(rest) == 1:
                        for stmt in tree.body:
                            if not isinstance(stmt, ast.ImportFrom):
                                continue
                            for alias in stmt.names:
                                if (alias.asname or alias.name) != rest[0]:
                                    continue
                                package = parts[:split]
                                if path.name not in {"__init__.py", "__init__.pyi"}:
                                    package = package[:-1]
                                prefix = (
                                    package[: len(package) - stmt.level + 1]
                                    if stmt.level
                                    else []
                                )
                                target = ".".join(
                                    prefix
                                    + ([stmt.module] if stmt.module else [])
                                    + [alias.name]
                                )
                                found = self.lookup(target, seen)
                                if found:
                                    return found
        return {}

    def _tree(self, path: Path) -> ast.Module | None:
        if path in self.cache:
            return self.cache[path]
        if not path.is_file():
            return None
        try:
            with tokenize.open(path) as stream:
                tree = ast.parse(stream.read())
        except (SyntaxError, OSError, UnicodeError):
            tree = None
        self.cache[path] = tree
        return tree
