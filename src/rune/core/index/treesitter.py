"""Tree-sitter based symbol/import extraction.

`ParserAdapter` (ARCHITECTURE.md §4.2) fixes only the *output shape*
(list[Symbol], list[RawImport]) — each language decides its own
`qualified_name` algorithm and AST walk. Do not expect Python/JavaScript/
TypeScript to share a walking algorithm; their grammars use different node
type names for the same concepts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

import tree_sitter_javascript as _ts_javascript
import tree_sitter_python as _ts_python
import tree_sitter_typescript as _ts_typescript
from tree_sitter import Language, Node, Parser

from rune.core.storage.models import Symbol, SymbolKind

_PY_LANGUAGE = Language(_ts_python.language())
_JS_LANGUAGE = Language(_ts_javascript.language())
_TS_LANGUAGE = Language(_ts_typescript.language_typescript())
_TSX_LANGUAGE = Language(_ts_typescript.language_tsx())


@dataclass(frozen=True)
class RawImport:
    """A not-yet-resolved import as written in source. `imports.py`
    resolves `specifier` to a repo file (or leaves it unresolved for
    external packages) — this module never touches the filesystem beyond
    reading the one file it's parsing.
    """

    specifier: str
    line: int


class ParserAdapter(Protocol):
    def extract_symbols(self, path: str, source: bytes) -> list[Symbol]: ...
    def extract_imports(self, path: str, source: bytes) -> list[RawImport]: ...


def symbol_id(path: str, qualified_name: str, kind: SymbolKind) -> str:
    """Stable id: pure function of (path, qualified_name, kind) —
    DATA_MODEL.md §2.2. A rename changes qualified_name and therefore
    produces a new id; V1 does not attempt fuzzy rename tracking.
    """
    digest = hashlib.sha1(
        f"{path}:{qualified_name}:{kind.value}".encode()
    ).hexdigest()
    return digest[:16]


def _header_text(node: Node, source: bytes) -> str | None:
    """Text from the node's start up to (not including) its `body` field —
    a reasonable stand-in for "signature" across function/class/interface
    declarations without per-language special-casing.
    """
    body = node.child_by_field_name("body")
    if body is None:
        return None
    header = source[node.start_byte : body.start_byte].decode("utf-8", errors="replace")
    return header.rstrip().rstrip(":").strip() or None


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


class PythonParserAdapter:
    def __init__(self) -> None:
        self._parser = Parser(_PY_LANGUAGE)

    def extract_symbols(self, path: str, source: bytes) -> list[Symbol]:
        tree = self._parser.parse(source)
        symbols: list[Symbol] = []
        self._walk(tree.root_node, path, source, [], symbols)
        return symbols

    def _walk(
        self,
        node: Node,
        path: str,
        source: bytes,
        scope: list[str],
        symbols: list[Symbol],
    ) -> None:
        for child in node.children:
            if child.type == "class_definition":
                name = child.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(path, name, qualified_name, SymbolKind.class_, child, source)
                )
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk(body, path, source, [*scope, name], symbols)
            elif child.type == "function_definition":
                name = child.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                kind = SymbolKind.method if scope else SymbolKind.function
                symbols.append(
                    self._make_symbol(path, name, qualified_name, kind, child, source)
                )
                # V1 does not descend into function bodies: nested defs/
                # classes local to a function are out of scope.
            elif not scope and child.type == "expression_statement":
                self._maybe_module_variable(child, path, symbols)

    def _make_symbol(
        self,
        path: str,
        name: str,
        qualified_name: str,
        kind: SymbolKind,
        node: Node,
        source: bytes,
    ) -> Symbol:
        return Symbol(
            symbol_id=symbol_id(path, qualified_name, kind),
            file=path,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            signature=_header_text(node, source),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )

    def _maybe_module_variable(
        self, expr_stmt: Node, path: str, symbols: list[Symbol]
    ) -> None:
        if len(expr_stmt.children) != 1 or expr_stmt.children[0].type != "assignment":
            return
        assignment = expr_stmt.children[0]
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            return
        name = left.text.decode("utf-8")
        kind = SymbolKind.constant if name.isupper() else SymbolKind.variable
        symbols.append(
            Symbol(
                symbol_id=symbol_id(path, name, kind),
                file=path,
                name=name,
                qualified_name=name,
                kind=kind,
                signature=None,
                start_line=assignment.start_point[0] + 1,
                end_line=assignment.end_point[0] + 1,
            )
        )

    def extract_imports(self, path: str, source: bytes) -> list[RawImport]:
        tree = self._parser.parse(source)
        imports: list[RawImport] = []
        self._collect_imports(tree.root_node, imports)
        return imports

    def _collect_imports(self, node: Node, imports: list[RawImport]) -> None:
        if node.type == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "identifier"):
                    imports.append(
                        RawImport(specifier=child.text.decode("utf-8"), line=child.start_point[0] + 1)
                    )
                elif child.type == "aliased_import":
                    dotted = child.children[0] if child.children else None
                    if dotted is not None:
                        imports.append(
                            RawImport(
                                specifier=dotted.text.decode("utf-8"),
                                line=child.start_point[0] + 1,
                            )
                        )
        elif node.type == "import_from_statement":
            module = node.child_by_field_name("module_name")
            if module is not None:
                imports.append(
                    RawImport(specifier=module.text.decode("utf-8"), line=module.start_point[0] + 1)
                )
        for child in node.children:
            self._collect_imports(child, imports)


# --------------------------------------------------------------------------
# JavaScript / TypeScript (shared walk — same grammar family, TS/TSX just
# add interface/type_alias on top of the JS node types)
# --------------------------------------------------------------------------


class _JsFamilyParserAdapter:
    def __init__(self, language: Language) -> None:
        self._parser = Parser(language)

    def extract_symbols(self, path: str, source: bytes) -> list[Symbol]:
        tree = self._parser.parse(source)
        symbols: list[Symbol] = []
        self._walk(tree.root_node, path, source, [], symbols)
        return symbols

    def _walk(
        self,
        node: Node,
        path: str,
        source: bytes,
        scope: list[str],
        symbols: list[Symbol],
    ) -> None:
        for child in node.children:
            if child.type == "class_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = name_node.text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(path, name, qualified_name, SymbolKind.class_, child, source)
                )
                body = child.child_by_field_name("body")
                if body is not None:
                    self._walk(body, path, source, [*scope, name], symbols)
            elif child.type == "method_definition":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = name_node.text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(path, name, qualified_name, SymbolKind.method, child, source)
                )
            elif child.type == "function_declaration":
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                name = name_node.text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(path, name, qualified_name, SymbolKind.function, child, source)
                )
            elif child.type == "interface_declaration":
                name = child.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(path, name, qualified_name, SymbolKind.interface, child, source)
                )
            elif child.type == "type_alias_declaration":
                name = child.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    Symbol(
                        symbol_id=symbol_id(path, qualified_name, SymbolKind.type),
                        file=path,
                        name=name,
                        qualified_name=qualified_name,
                        kind=SymbolKind.type,
                        signature=None,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
            elif child.type == "lexical_declaration" and not scope:
                self._maybe_module_variable(child, path, symbols)
            else:
                self._walk(child, path, source, scope, symbols)

    def _make_symbol(
        self,
        path: str,
        name: str,
        qualified_name: str,
        kind: SymbolKind,
        node: Node,
        source: bytes,
    ) -> Symbol:
        return Symbol(
            symbol_id=symbol_id(path, qualified_name, kind),
            file=path,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            signature=_header_text(node, source),
            start_line=node.start_point[0] + 1,
            end_line=node.end_point[0] + 1,
        )

    def _maybe_module_variable(
        self, lexical_decl: Node, path: str, symbols: list[Symbol]
    ) -> None:
        is_const = lexical_decl.children[0].type == "const" if lexical_decl.children else False
        for child in lexical_decl.children:
            if child.type != "variable_declarator":
                continue
            name_node = child.child_by_field_name("name")
            value_node = child.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier":
                continue
            if value_node is not None and value_node.type in (
                "arrow_function",
                "function",
                "function_expression",
            ):
                # a function/arrow assigned to a const — treat as a function,
                # not a plain variable
                name = name_node.text.decode("utf-8")
                qualified_name = name
                symbols.append(
                    Symbol(
                        symbol_id=symbol_id(path, qualified_name, SymbolKind.function),
                        file=path,
                        name=name,
                        qualified_name=qualified_name,
                        kind=SymbolKind.function,
                        signature=None,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                    )
                )
                continue
            name = name_node.text.decode("utf-8")
            kind = SymbolKind.constant if is_const else SymbolKind.variable
            symbols.append(
                Symbol(
                    symbol_id=symbol_id(path, name, kind),
                    file=path,
                    name=name,
                    qualified_name=name,
                    kind=kind,
                    signature=None,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                )
            )

    def extract_imports(self, path: str, source: bytes) -> list[RawImport]:
        tree = self._parser.parse(source)
        imports: list[RawImport] = []
        self._collect_imports(tree.root_node, imports)
        return imports

    def _collect_imports(self, node: Node, imports: list[RawImport]) -> None:
        if node.type in ("import_statement", "export_statement"):
            source_node = node.child_by_field_name("source")
            if source_node is not None:
                specifier = _string_literal_value(source_node)
                if specifier is not None:
                    imports.append(RawImport(specifier=specifier, line=node.start_point[0] + 1))
        elif node.type == "call_expression":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" and fn.text == b"require":
                args = node.child_by_field_name("arguments")
                if args is not None and args.named_children:
                    specifier = _string_literal_value(args.named_children[0])
                    if specifier is not None:
                        imports.append(
                            RawImport(specifier=specifier, line=node.start_point[0] + 1)
                        )
        for child in node.children:
            self._collect_imports(child, imports)


def _string_literal_value(node: Node) -> str | None:
    if node.type != "string":
        return None
    for child in node.children:
        if child.type == "string_fragment":
            return child.text.decode("utf-8")
    return None


class JavaScriptParserAdapter(_JsFamilyParserAdapter):
    def __init__(self) -> None:
        super().__init__(_JS_LANGUAGE)


class TypeScriptParserAdapter(_JsFamilyParserAdapter):
    def __init__(self) -> None:
        super().__init__(_TS_LANGUAGE)


class TsxParserAdapter(_JsFamilyParserAdapter):
    def __init__(self) -> None:
        super().__init__(_TSX_LANGUAGE)


_ADAPTERS: dict[str, ParserAdapter] = {}


def get_parser_adapter(language: str, path: str = "") -> ParserAdapter:
    """Returns a cached adapter instance for `language`. `path` is only
    used to distinguish `.tsx` (JSX-flavored grammar) from plain `.ts`
    within the "typescript" language bucket.
    """
    key = language
    if language == "typescript" and path.endswith(".tsx"):
        key = "tsx"
    if key not in _ADAPTERS:
        if key == "python":
            _ADAPTERS[key] = PythonParserAdapter()
        elif key == "javascript":
            _ADAPTERS[key] = JavaScriptParserAdapter()
        elif key == "typescript":
            _ADAPTERS[key] = TypeScriptParserAdapter()
        elif key == "tsx":
            _ADAPTERS[key] = TsxParserAdapter()
        else:
            raise ValueError(f"no ParserAdapter for language {language!r}")
    return _ADAPTERS[key]
