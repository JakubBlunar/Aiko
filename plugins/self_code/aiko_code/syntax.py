"""Syntax-backed declarations and identifier candidates, without project execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import lru_cache
import io
import keyword
from pathlib import PurePosixPath
import re
import tokenize

from .policy import Source

SUPPORTED = frozenset({".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
MAX_NODES = 50000
MAX_SYMBOLS = 500
MAX_IDENTIFIERS = 15000


@dataclass
class Parsed:
    language: str
    parser: str
    symbols: list[dict] = field(default_factory=list)
    identifiers: list[dict] = field(default_factory=list)
    problems: list[dict] = field(default_factory=list)
    complete: bool = True


def _symbol(source: Source, name, kind, scope, line, column, start, end):
    lines = source.lines
    qualified = ".".join((*scope, name))
    return {
        **source.identity(),
        "name": name[:200],
        "kind": kind,
        "qualified_name": qualified[:512],
        "name_complete": len(name) <= 200 and len(qualified) <= 512,
        "line": line,
        "column": column,
        "start_line": start,
        "end_line": end,
        "signature": lines[line - 1].strip()[:240] if line <= len(lines) else "",
    }


def _python(source: Source) -> Parsed:
    result = Parsed("python", "python_ast_and_tokenize")
    try:
        tree = ast.parse(source.text, filename=source.path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        result.complete = False
        result.problems.append(
            {
                "line": getattr(exc, "lineno", None) or 1,
                "column": getattr(exc, "offset", None) or 1,
                "message": str(getattr(exc, "msg", None) or "Source could not be parsed")[:300],
                "severity": "error",
                "kind": "syntax",
            }
        )
        tree = None
    pending = [(tree, (), False)] if tree is not None else []
    count = 0
    ast_identifiers = []

    def char_column(line, byte_column):
        return len(source.lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8")) + 1

    while pending:
        node, scope, in_function = pending.pop()
        count += 1
        if count > MAX_NODES or len(result.symbols) >= MAX_SYMBOLS:
            result.complete = False
            break
        child_scope, child_function = scope, in_function
        if isinstance(node, ast.Name):
            ast_identifiers.append(
                {
                    "name": node.id,
                    "line": node.lineno,
                    "column": char_column(node.lineno, node.col_offset),
                }
            )
        elif isinstance(node, ast.Attribute) and node.end_lineno and node.end_col_offset:
            begin = node.end_col_offset - len(node.attr.encode("utf-8"))
            ast_identifiers.append(
                {
                    "name": node.attr,
                    "line": node.end_lineno,
                    "column": char_column(node.end_lineno, begin),
                }
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            decorators = getattr(node, "decorator_list", [])
            start = min([node.lineno] + [d.lineno for d in decorators])
            line = source.lines[node.lineno - 1]
            declaration = re.search(r"\b(?:class|def)\s+(" + re.escape(node.name) + r")\b", line)
            column = declaration.start(1) + 1 if declaration else 1
            result.symbols.append(
                _symbol(
                    source,
                    node.name,
                    kind,
                    scope,
                    node.lineno,
                    max(1, column),
                    start,
                    node.end_lineno or node.lineno,
                )
            )
            child_scope = (*scope, node.name)
            child_function = kind == "function"
        elif not in_function and isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        result.symbols.append(
                            _symbol(
                                source,
                                name.id,
                                "variable",
                                scope,
                                name.lineno,
                                char_column(name.lineno, name.col_offset),
                                node.lineno,
                                node.end_lineno or node.lineno,
                            )
                        )
        for child in reversed(list(ast.iter_child_nodes(node))):
            pending.append((child, child_scope, child_function))
    try:
        for token in tokenize.generate_tokens(io.StringIO(source.text).readline):
            if token.type == tokenize.NAME and not keyword.iskeyword(token.string):
                if len(result.identifiers) >= MAX_IDENTIFIERS:
                    result.complete = False
                    break
                result.identifiers.append(
                    {"name": token.string, "line": token.start[0], "column": token.start[1] + 1}
                )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        result.complete = False
    # AST names include expressions inside f-strings on Python versions whose
    # tokenizer presents the complete f-string as one STRING token.
    combined = {
        (r["line"], r["column"], r["name"]): r for r in [*result.identifiers, *ast_identifiers]
    }
    result.identifiers = [combined[key] for key in sorted(combined)][:MAX_IDENTIFIERS]
    if len(combined) > MAX_IDENTIFIERS:
        result.complete = False
    result.symbols.sort(key=lambda item: (item["line"], item["column"]))
    result.complete = result.complete and all(s["name_complete"] for s in result.symbols)
    return result


@lru_cache(maxsize=3)
def _language(name):
    from tree_sitter import Language

    if name == "javascript":
        import tree_sitter_javascript

        return Language(tree_sitter_javascript.language())
    import tree_sitter_typescript

    grammar = (
        tree_sitter_typescript.language_tsx
        if name == "tsx"
        else tree_sitter_typescript.language_typescript
    )
    return Language(grammar())


def _typescript(source: Source, language: str) -> Parsed:
    from tree_sitter import Parser

    data = source.text.encode("utf-8")
    root = Parser(_language(language)).parse(data).root_node
    result = Parsed(language, "tree_sitter_syntax", complete=not root.has_error)
    declarations = {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "method_definition": "method",
        "method_signature": "method",
        "public_field_definition": "field",
        "property_signature": "field",
    }
    identifiers = {
        "identifier",
        "property_identifier",
        "type_identifier",
        "private_property_identifier",
        "shorthand_property_identifier",
        "shorthand_property_identifier_pattern",
    }

    def text(node):
        return data[node.start_byte : node.end_byte].decode("utf-8")

    def location(node):
        # Tree-sitter columns are UTF-8 bytes. Public columns are characters,
        # including after non-ASCII comments or identifiers on the same line.
        start = data.rfind(b"\n", 0, node.start_byte) + 1
        return node.start_point.row + 1, len(data[start : node.start_byte].decode("utf-8")) + 1

    pending, count = [(root, (), False)], 0
    while pending:
        node, scope, in_function = pending.pop()
        count += 1
        if (
            count > MAX_NODES
            or len(result.symbols) >= MAX_SYMBOLS
            or len(result.identifiers) >= MAX_IDENTIFIERS
        ):
            result.complete = False
            break
        line, column = location(node)
        if (node.is_error or node.is_missing) and len(result.problems) < 30:
            result.problems.append(
                {
                    "line": line,
                    "column": column,
                    "severity": "error",
                    "kind": "syntax",
                    "message": "Missing syntax" if node.is_missing else "Invalid syntax",
                }
            )
        if node.type in identifiers:
            result.identifiers.append({"name": text(node), "line": line, "column": column})
        kind = declarations.get(node.type)
        if node.type == "property_signature" and (in_function or not scope):
            kind = None  # An anonymous prop type is not a member of its enclosing function.
        value = node.child_by_field_name("value")
        if node.type == "variable_declarator":
            callable_value = value and value.type in {
                "arrow_function",
                "function_expression",
                "generator_function",
                "class",
            }
            if not in_function or callable_value:
                kind = "function" if callable_value else "variable"
        name = node.child_by_field_name("name")
        child_scope, child_function = scope, in_function
        if kind and name and name.type in identifiers | {"string", "number"}:
            name_text = text(name)
            name_line, name_column = location(name)
            end_line = node.end_point.row + (1 if node.end_point.column else 0)
            result.symbols.append(
                _symbol(
                    source,
                    name_text,
                    kind,
                    scope,
                    name_line,
                    name_column,
                    line,
                    max(line, end_line),
                )
            )
            if kind in {"class", "interface", "type", "function", "method", "enum"}:
                child_scope = (*scope, name_text)
                child_function = kind in {"function", "method"}
        # Anonymous callbacks also establish a function body, without inventing
        # a declaration name or turning their locals into module symbols.
        if node.type in {"arrow_function", "function_expression", "generator_function"}:
            child_function = True
        pending.extend(
            (child, child_scope, child_function) for child in reversed(node.named_children)
        )
    result.symbols.sort(key=lambda item: (item["line"], item["column"]))
    result.complete = result.complete and all(s["name_complete"] for s in result.symbols)
    return result


def parse(source: Source) -> Parsed:
    suffix = PurePosixPath(source.path).suffix.lower()
    if suffix in {".py", ".pyi"}:
        return _python(source)
    if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
        return _typescript(source, "javascript")
    if suffix in {".ts", ".mts", ".cts", ".tsx"}:
        return _typescript(source, "tsx" if suffix == ".tsx" else "typescript")
    return Parsed("unsupported", "none", complete=False)
