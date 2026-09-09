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

from rune.core.storage.models import EdgeType, Symbol, SymbolKind

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


@dataclass(frozen=True)
class RawReference:
    """A not-yet-resolved call/inheritance reference as written in source.
    `core.index.references` resolves `name` against this file's own
    symbols and the symbols of files it imports (or leaves it unresolved)
    — this module never does that resolution itself, only extraction.
    """

    name: str
    edge_type: EdgeType  # calls | extends | implements
    line: int


class ParserAdapter(Protocol):
    def extract_symbols(self, path: str, source: bytes) -> list[Symbol]: ...
    def extract_imports(self, path: str, source: bytes) -> list[RawImport]: ...
    def extract_references(self, path: str, source: bytes) -> list[RawReference]:
        """Best-effort call/inheritance sites (ARCHITECTURE.md §4.3): a
        call expression's target name, or a class's superclass/interface
        name. Purely syntactic — no attempt at type inference or binding
        resolution, which is why resolution (in core.index.references) is
        confidence-scored and allowed to come back unresolved.
        """
        ...
    def qualified_name(self, node: Node) -> str:
        """Given an arbitrary tree-sitter node from this language's
        grammar (e.g. one `core.index.references` finds independently in
        Milestone 3, not necessarily one `extract_symbols` already
        visited), computes the same dotted qualified name `extract_symbols`
        would assign it — walking up the parent chain accumulating
        enclosing class names, rather than the top-down scope-stack
        threading `extract_symbols` uses internally. Both paths must agree
        on the same name for the same node; see the cross-check test in
        tests/unit/test_treesitter.py.
        """
        ...
    def has_syntax_error(self, source: bytes) -> bool:
        """True if tree-sitter's error-tolerant parser had to fall back to
        an ERROR/MISSING node anywhere in the tree. `core.update` uses
        this to mark the file `parse_error` even though `extract_symbols`
        didn't raise and may have returned partial/best-effort symbols —
        tree-sitter recovers from syntax errors by producing a tree, it
        does not raise, so this is the only way to detect degraded output.
        """
        ...


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

    def has_syntax_error(self, source: bytes) -> bool:
        return self._parser.parse(source).root_node.has_error

    def qualified_name(self, node: Node) -> str:
        parts: list[str] = []
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            parts.append(name_node.text.decode("utf-8"))
        current = node.parent
        while current is not None:
            if current.type == "class_definition":
                cls_name = current.child_by_field_name("name")
                if cls_name is not None:
                    parts.append(cls_name.text.decode("utf-8"))
            current = current.parent
        return ".".join(reversed(parts))

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
            # `@decorator\ndef foo(): ...` / `@decorator\nclass Foo: ...`
            # wraps the real function_definition/class_definition inside a
            # decorated_definition node — unwrap it so decorated symbols
            # (very common: @staticmethod, @property, @dataclass, route
            # decorators, pytest fixtures, ...) aren't silently skipped.
            # The decorator line(s) count as part of the symbol's range.
            range_node = child
            target = child
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition")
                if inner is None:
                    continue
                target = inner

            if target.type == "class_definition":
                name = target.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                symbols.append(
                    self._make_symbol(
                        path, name, qualified_name, SymbolKind.class_, target, source, range_node
                    )
                )
                body = target.child_by_field_name("body")
                if body is not None:
                    self._walk(body, path, source, [*scope, name], symbols)
            elif target.type == "function_definition":
                name = target.child_by_field_name("name").text.decode("utf-8")
                qualified_name = ".".join([*scope, name])
                kind = SymbolKind.method if scope else SymbolKind.function
                symbols.append(
                    self._make_symbol(path, name, qualified_name, kind, target, source, range_node)
                )
                # V1 does not descend into function bodies: nested defs/
                # classes local to a function are out of scope.
            elif not scope and target.type == "expression_statement":
                self._maybe_module_variable(target, path, symbols)
            else:
                # Block statements (`if`/`try`/`with`/`match`/...) can nest
                # class/def at module or class level — recurse so they aren't
                # silently missed. Function bodies are still never descended
                # into (the `function_definition` branch above returns without
                # recursing, so V1 keeps ignoring function-local nested defs).
                self._walk(child, path, source, scope, symbols)

    def _make_symbol(
        self,
        path: str,
        name: str,
        qualified_name: str,
        kind: SymbolKind,
        node: Node,
        source: bytes,
        range_node: Node | None = None,
    ) -> Symbol:
        range_node = range_node or node
        return Symbol(
            symbol_id=symbol_id(path, qualified_name, kind),
            file=path,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            signature=_header_text(node, source),
            start_line=range_node.start_point[0] + 1,
            end_line=range_node.end_point[0] + 1,
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

    def extract_references(self, path: str, source: bytes) -> list[RawReference]:
        tree = self._parser.parse(source)
        refs: list[RawReference] = []
        self._collect_references(tree.root_node, refs)
        return refs

    def _collect_references(self, node: Node, refs: list[RawReference]) -> None:
        if node.type == "call":
            fn = node.child_by_field_name("function")
            name = self._call_target_name(fn) if fn is not None else None
            if name is not None:
                refs.append(
                    RawReference(name=name, edge_type=EdgeType.calls, line=node.start_point[0] + 1)
                )
        elif node.type == "class_definition":
            superclasses = node.child_by_field_name("superclasses")
            if superclasses is not None:
                for child in superclasses.children:
                    # `_base_class_name` returns None for punctuation and for
                    # `metaclass=Meta`-style keyword_argument entries (neither
                    # is a real base class), so no extra type check is needed
                    # here beyond what that helper already filters.
                    name = self._base_class_name(child)
                    if name is not None:
                        refs.append(
                            RawReference(
                                name=name,
                                edge_type=EdgeType.extends,
                                line=node.start_point[0] + 1,
                            )
                        )
        for child in node.children:
            self._collect_references(child, refs)

    @staticmethod
    def _call_target_name(fn_node: Node) -> str | None:
        if fn_node.type == "identifier":
            return fn_node.text.decode("utf-8")
        if fn_node.type == "attribute":
            attr = fn_node.child_by_field_name("attribute")
            if attr is not None:
                return attr.text.decode("utf-8")
        return None

    @classmethod
    def _base_class_name(cls, node: Node) -> str | None:
        """Best-effort name for a base-class expression, so a reference is
        still *recorded* (per ARCHITECTURE.md §4.3, unresolved is never the
        same as dropped) even when the base isn't a bare identifier:
        `pkg.Base` -> "Base" (rightmost attribute, same convention already
        used for `self.db.fetch()`-style call targets), `Base[T]`/
        `pkg.Base[T]` -> recurses into the subscripted value. Returns None
        only for genuinely non-reference children (punctuation, a
        `metaclass=Meta` keyword_argument), which the caller drops.
        """
        if node.type == "identifier":
            return node.text.decode("utf-8")
        if node.type == "attribute":
            attr = node.child_by_field_name("attribute")
            return attr.text.decode("utf-8") if attr is not None else None
        if node.type == "subscript":
            value = node.child_by_field_name("value")
            return cls._base_class_name(value) if value is not None else None
        return None


# --------------------------------------------------------------------------
# JavaScript / TypeScript (shared walk — same grammar family, TS/TSX just
# add interface/type_alias on top of the JS node types)
# --------------------------------------------------------------------------


class _JsFamilyParserAdapter:
    def __init__(self, language: Language) -> None:
        self._parser = Parser(language)

    def has_syntax_error(self, source: bytes) -> bool:
        return self._parser.parse(source).root_node.has_error

    def qualified_name(self, node: Node) -> str:
        parts: list[str] = []
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            parts.append(name_node.text.decode("utf-8"))
        current = node.parent
        while current is not None:
            if current.type == "class_declaration":
                cls_name = current.child_by_field_name("name")
                if cls_name is not None:
                    parts.append(cls_name.text.decode("utf-8"))
            current = current.parent
        return ".".join(reversed(parts))

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
            elif child.type in ("lexical_declaration", "variable_declaration") and not scope:
                # `variable_declaration` is `var ...` (its own node type,
                # distinct from `let`/`const`'s `lexical_declaration`) —
                # both need the same variable/constant extraction.
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

    def extract_references(self, path: str, source: bytes) -> list[RawReference]:
        tree = self._parser.parse(source)
        refs: list[RawReference] = []
        self._collect_references(tree.root_node, refs)
        return refs

    def _collect_references(self, node: Node, refs: list[RawReference]) -> None:
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            name = self._call_target_name(fn) if fn is not None else None
            if name is not None:
                refs.append(
                    RawReference(name=name, edge_type=EdgeType.calls, line=node.start_point[0] + 1)
                )
        elif node.type == "class_declaration":
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            if heritage is not None:
                for clause in heritage.children:
                    if clause.type == "extends_clause":
                        value = clause.child_by_field_name("value")
                        name = self._heritage_type_name(value) if value is not None else None
                        if name is not None:
                            refs.append(
                                RawReference(
                                    name=name,
                                    edge_type=EdgeType.extends,
                                    line=node.start_point[0] + 1,
                                )
                            )
                    elif clause.type == "implements_clause":
                        for c in clause.children:
                            name = self._heritage_type_name(c)
                            if name is not None:
                                refs.append(
                                    RawReference(
                                        name=name,
                                        edge_type=EdgeType.implements,
                                        line=node.start_point[0] + 1,
                                    )
                                )
        for child in node.children:
            self._collect_references(child, refs)

    @staticmethod
    def _call_target_name(fn_node: Node) -> str | None:
        if fn_node.type == "identifier":
            return fn_node.text.decode("utf-8")
        if fn_node.type == "member_expression":
            prop = fn_node.child_by_field_name("property")
            if prop is not None:
                return prop.text.decode("utf-8")
        return None

    @classmethod
    def _heritage_type_name(cls, node: Node) -> str | None:
        """Best-effort name for an `extends`/`implements` type expression,
        so a reference is still *recorded* (ARCHITECTURE.md §4.3: unresolved
        is never the same as dropped) even when it isn't a bare identifier:
        `ns.Base` -> "Base" (rightmost property, same convention as
        `_call_target_name`'s member-expression handling), `ns.Shape` (a
        `nested_type_identifier`, distinct from `member_expression` in this
        grammar) -> "Shape", `Comparable<Foo>`/`Base<T>` (`generic_type`)
        -> recurses into the un-parameterized name. Returns None only for
        punctuation (commas between `implements` entries).
        """
        if node.type in ("identifier", "type_identifier"):
            return node.text.decode("utf-8")
        if node.type == "member_expression":
            prop = node.child_by_field_name("property")
            return prop.text.decode("utf-8") if prop is not None else None
        if node.type == "nested_type_identifier":
            name = node.child_by_field_name("name")
            return name.text.decode("utf-8") if name is not None else None
        if node.type == "generic_type":
            name = node.child_by_field_name("name")
            return cls._heritage_type_name(name) if name is not None else None
        return None


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
