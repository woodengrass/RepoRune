from __future__ import annotations

from rune.core.index.treesitter import (
    JavaScriptParserAdapter,
    PythonParserAdapter,
    TypeScriptParserAdapter,
    get_parser_adapter,
    symbol_id,
)
from rune.core.storage.models import EdgeType, SymbolKind

PY_SOURCE = b"""
CONST_VALUE = 42

class AuthService:
    def __init__(self, db):
        self.db = db

    def login(self, user):
        return user

def helper():
    pass

variable_x = compute()
"""


def test_python_extracts_decorated_methods_and_classes() -> None:
    """Regression test: @staticmethod/@property/@dataclass etc. wrap the
    real function_definition/class_definition inside a decorated_definition
    node in tree-sitter-python. An earlier version of the walker only
    matched bare function_definition/class_definition children, silently
    skipping every decorated symbol -- which in real Python code (routes,
    properties, dataclasses, fixtures) is most of them.
    """
    source = b"""
class Foo:
    @staticmethod
    def bar():
        pass

    @property
    def baz(self):
        return 1

@dataclass
class Config:
    pass
"""
    adapter = PythonParserAdapter()
    symbols = adapter.extract_symbols("a.py", source)
    by_qname = {s.qualified_name: s for s in symbols}

    assert by_qname["Foo.bar"].kind == SymbolKind.method
    assert by_qname["Foo.baz"].kind == SymbolKind.method
    assert by_qname["Config"].kind == SymbolKind.class_
    # the decorator line counts as part of the symbol's range
    assert by_qname["Foo.bar"].start_line == 3  # the @staticmethod line
    assert by_qname["Config"].start_line == 11  # the @dataclass line


def test_python_extracts_module_class_method_function_and_variables() -> None:
    adapter = PythonParserAdapter()
    symbols = adapter.extract_symbols("app/service.py", PY_SOURCE)
    by_qname = {s.qualified_name: s for s in symbols}

    assert by_qname["AuthService"].kind == SymbolKind.class_
    assert by_qname["AuthService.__init__"].kind == SymbolKind.method
    assert by_qname["AuthService.login"].kind == SymbolKind.method
    assert by_qname["helper"].kind == SymbolKind.function
    assert by_qname["CONST_VALUE"].kind == SymbolKind.constant
    assert by_qname["variable_x"].kind == SymbolKind.variable


def test_python_symbol_id_is_stable_pure_function_of_path_qname_kind() -> None:
    adapter = PythonParserAdapter()
    a = adapter.extract_symbols("app/service.py", PY_SOURCE)
    b = adapter.extract_symbols("app/service.py", PY_SOURCE)
    assert {s.symbol_id for s in a} == {s.symbol_id for s in b}


def test_python_rename_produces_a_different_symbol_id() -> None:
    adapter = PythonParserAdapter()
    before = adapter.extract_symbols("app/service.py", PY_SOURCE)
    renamed_source = PY_SOURCE.replace(b"def login", b"def authenticate")
    after = adapter.extract_symbols("app/service.py", renamed_source)

    before_ids = {s.qualified_name: s.symbol_id for s in before}
    after_ids = {s.qualified_name: s.symbol_id for s in after}

    assert "AuthService.login" not in after_ids
    assert "AuthService.authenticate" in after_ids
    assert after_ids["AuthService.authenticate"] != before_ids["AuthService.login"]
    # unrelated symbols keep the same id (a rename doesn't churn the file)
    assert after_ids["AuthService"] == before_ids["AuthService"]


def test_python_extracts_absolute_and_relative_imports() -> None:
    adapter = PythonParserAdapter()
    source = b"import os.path\nfrom .services import UserService\nfrom ..pkg import thing\n"
    imports = adapter.extract_imports("app/main.py", source)
    specifiers = {i.specifier for i in imports}
    assert "os.path" in specifiers
    assert ".services" in specifiers
    assert "..pkg" in specifiers


def test_python_extracts_call_and_attribute_call_references() -> None:
    source = b"""
class Foo(Base):
    def method(self):
        helper()
        self.other()
        obj.thing()
"""
    adapter = PythonParserAdapter()
    refs = adapter.extract_references("a.py", source)
    calls = {r.name for r in refs if r.edge_type == EdgeType.calls}
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    assert calls == {"helper", "other", "thing"}
    assert extends == {"Base"}


def test_python_extends_ignores_keyword_arguments() -> None:
    """`class Foo(Base, metaclass=Meta):` -- only `Base` is a real
    superclass reference; `metaclass=Meta` is a keyword_argument node, not
    a plain identifier, and must not be reported as an `extends` edge.
    """
    adapter = PythonParserAdapter()
    refs = adapter.extract_references("a.py", b"class Foo(Base, metaclass=Meta):\n    pass\n")
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    assert extends == {"Base"}


def test_python_multiple_inheritance_produces_one_extends_ref_per_base() -> None:
    adapter = PythonParserAdapter()
    refs = adapter.extract_references("a.py", b"class Foo(Base1, Base2):\n    pass\n")
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    assert extends == {"Base1", "Base2"}


def test_python_qualified_and_generic_bases_still_produce_extends_refs() -> None:
    """Regression test: `class Foo(pkg.Base):` and `class Bar(Base[T]):`
    used to be silently dropped -- the walker only matched a bare
    `identifier` superclass node, so a qualified (`attribute`) or generic
    (`subscript`) base produced no RawReference at all. That violates
    ARCHITECTURE.md §4.3: an unresolvable reference must still be
    *recorded* (as unresolved), never dropped outright, the same way an
    unresolved import is kept with `target_file=None` rather than omitted.
    Best-effort name extraction takes the rightmost/innermost identifier
    (`pkg.Base` -> "Base", `pkg.sub.Base[T]` -> "Base"), mirroring the
    existing convention for `self.db.fetch()`-style call targets.
    """
    adapter = PythonParserAdapter()
    refs = adapter.extract_references(
        "a.py",
        b"class Foo(pkg.Base):\n    pass\n\n"
        b"class Bar(Base[T]):\n    pass\n\n"
        b"class Baz(pkg.sub.Base[T]):\n    pass\n",
    )
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    assert extends == {"Base"}
    assert len(refs) == 3  # each class still contributes its own reference


def test_typescript_multiple_implements_produces_one_ref_per_interface() -> None:
    adapter = TypeScriptParserAdapter()
    refs = adapter.extract_references(
        "a.ts", b"class Foo implements A, B {\n}\n"
    )
    implements = {r.name for r in refs if r.edge_type == EdgeType.implements}
    assert implements == {"A", "B"}


def test_typescript_qualified_and_generic_heritage_still_produce_refs() -> None:
    """Regression test: `extends ns.Base` (a `member_expression`) and
    `implements ns.Shape` (a `nested_type_identifier`, a distinct grammar
    node from `member_expression`) used to be silently dropped -- only
    bare `identifier`/`type_identifier` heritage values were recorded. Also
    covers `implements Comparable<Foo>` (`generic_type`), which must
    recurse to the un-parameterized name rather than being dropped too.
    """
    adapter = TypeScriptParserAdapter()
    refs = adapter.extract_references(
        "a.ts",
        b"class Foo extends ns.Base implements ns.Shape, Comparable<Foo> {\n}\n",
    )
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    implements = {r.name for r in refs if r.edge_type == EdgeType.implements}
    assert extends == {"Base"}
    assert implements == {"Shape", "Comparable"}


def test_javascript_extracts_function_class_method_and_arrow_const() -> None:
    source = b"""
function add(a, b) { return a + b; }
class Widget {
  render() { return 1; }
}
const helper = () => 1;
const CONST_VAL = 1;
let mutableVal = 1;
"""
    adapter = JavaScriptParserAdapter()
    symbols = adapter.extract_symbols("src/widget.js", source)
    by_qname = {s.qualified_name: s for s in symbols}

    assert by_qname["add"].kind == SymbolKind.function
    assert by_qname["Widget"].kind == SymbolKind.class_
    assert by_qname["Widget.render"].kind == SymbolKind.method
    assert by_qname["helper"].kind == SymbolKind.function  # arrow fn, not a plain variable
    assert by_qname["CONST_VAL"].kind == SymbolKind.constant
    assert by_qname["mutableVal"].kind == SymbolKind.variable


def test_javascript_extracts_var_declarations_too() -> None:
    """Regression test: `var x = 1` parses as a `variable_declaration`
    node, a different type than `let`/`const`'s `lexical_declaration` --
    an earlier version only matched `lexical_declaration` and silently
    dropped every `var` at module level.
    """
    adapter = JavaScriptParserAdapter()
    symbols = adapter.extract_symbols("a.js", b"var oldStyle = 1;\n")
    by_qname = {s.qualified_name: s for s in symbols}
    assert by_qname["oldStyle"].kind == SymbolKind.variable


def test_javascript_extracts_export_default_class_and_function() -> None:
    source = b"""
export default class DefaultExport {
  render() {}
}
export default function defaultFn() {}
"""
    adapter = JavaScriptParserAdapter()
    symbols = adapter.extract_symbols("a.js", source)
    by_qname = {s.qualified_name: s for s in symbols}
    assert by_qname["DefaultExport"].kind == SymbolKind.class_
    assert by_qname["defaultFn"].kind == SymbolKind.function


def test_javascript_extracts_import_and_require_specifiers() -> None:
    source = b"""
import { foo } from "./foo";
import bar from "../bar";
const pkg = require("some-package");
"""
    adapter = JavaScriptParserAdapter()
    imports = adapter.extract_imports("src/widget.js", source)
    specifiers = {i.specifier for i in imports}
    assert specifiers == {"./foo", "../bar", "some-package"}


def test_javascript_extracts_call_and_member_call_references() -> None:
    source = b"""
function helper() {}
class Foo {
  method() {
    helper();
    this.other();
    obj.thing();
  }
}
"""
    adapter = JavaScriptParserAdapter()
    refs = adapter.extract_references("a.js", source)
    calls = {r.name for r in refs if r.edge_type == EdgeType.calls}
    assert calls == {"helper", "other", "thing"}


def test_typescript_extracts_extends_and_implements_references() -> None:
    source = b"""
interface Reducer { reduce(): number; }
class Base {}
class Foo extends Base implements Reducer {
  reduce(): number { return 0; }
}
"""
    adapter = TypeScriptParserAdapter()
    refs = adapter.extract_references("a.ts", source)
    extends = {r.name for r in refs if r.edge_type == EdgeType.extends}
    implements = {r.name for r in refs if r.edge_type == EdgeType.implements}
    assert extends == {"Base"}
    assert implements == {"Reducer"}


def test_typescript_extracts_interface_and_type_alias() -> None:
    source = b"""
interface Point { x: number; y: number; }
type ID = string | number;
function dist(a: Point): number { return a.x; }
"""
    adapter = TypeScriptParserAdapter()
    symbols = adapter.extract_symbols("src/geo.ts", source)
    by_qname = {s.qualified_name: s for s in symbols}

    assert by_qname["Point"].kind == SymbolKind.interface
    assert by_qname["ID"].kind == SymbolKind.type
    assert by_qname["dist"].kind == SymbolKind.function


def test_get_parser_adapter_returns_cached_instance_per_language() -> None:
    a = get_parser_adapter("python")
    b = get_parser_adapter("python")
    assert a is b


def test_get_parser_adapter_distinguishes_tsx_from_ts() -> None:
    ts_adapter = get_parser_adapter("typescript", "foo.ts")
    tsx_adapter = get_parser_adapter("typescript", "foo.tsx")
    assert ts_adapter is not tsx_adapter


def test_symbol_id_is_pure_function_of_inputs() -> None:
    assert symbol_id("a.py", "Foo.bar", SymbolKind.method) == symbol_id(
        "a.py", "Foo.bar", SymbolKind.method
    )
    assert symbol_id("a.py", "Foo.bar", SymbolKind.method) != symbol_id(
        "a.py", "Foo.baz", SymbolKind.method
    )


def test_python_has_syntax_error_true_for_malformed_source() -> None:
    """Regression test: tree-sitter recovers from syntax errors by
    producing a partial tree rather than raising, so `extract_symbols`
    alone can't tell the caller anything went wrong -- it can even return
    a symbol with a corrupted signature/range drawn from the malformed
    region. `has_syntax_error` is the dedicated way to detect this.
    """
    adapter = PythonParserAdapter()
    assert adapter.has_syntax_error(b"def foo(:\n    pass\n") is True
    assert adapter.has_syntax_error(b"def foo():\n    pass\n") is False


def test_javascript_has_syntax_error_true_for_malformed_source() -> None:
    adapter = JavaScriptParserAdapter()
    assert adapter.has_syntax_error(b"function foo( {\n") is True
    assert adapter.has_syntax_error(b"function foo() {}\n") is False


def _find_node(node, node_type: str):
    if node.type == node_type:
        return node
    for child in node.children:
        found = _find_node(child, node_type)
        if found is not None:
            return found
    return None


def test_python_qualified_name_matches_extract_symbols_for_same_node() -> None:
    """The two qualified-name computation paths -- extract_symbols's
    top-down scope-stack threading, and qualified_name's bottom-up parent
    walk (added for future callers like Milestone 3's reference resolver,
    which will hand it an arbitrary node rather than one it discovered
    itself top-down) -- must agree, or the same logical symbol would get
    two different ids depending on which path computed its name.
    """
    adapter = PythonParserAdapter()
    tree = adapter._parser.parse(PY_SOURCE)
    method_node = _find_node(tree.root_node, "function_definition")
    # PY_SOURCE's first function_definition encountered in a top-down walk
    # is AuthService.__init__ (module-level CONST_VALUE has no such node).
    from_extract = {s.qualified_name for s in adapter.extract_symbols("a.py", PY_SOURCE)}
    assert adapter.qualified_name(method_node) in from_extract


def test_python_extracts_symbols_inside_block_statements() -> None:
    """Regression test: `PythonParserAdapter._walk` had no generic recursion,
    so any class/def nested in an `if`/`try`/`with` block was silently missed
    (the JS-family walker already recursed). Block nesting must not hide
    symbols; function bodies are still out of scope by design.
    """
    source = (
        b"if True:\n"
        b"    class Hidden:\n"
        b"        pass\n"
        b"try:\n"
        b"    def helper():\n"
        b"        pass\n"
        b"except ImportError:\n"
        b"    def fallback():\n"
        b"        pass\n"
        b"class Foo:\n"
        b"    if True:\n"
        b"        def bar(self):\n"
        b"            pass\n"
        b"def outer():\n"
        b"    def inner():\n"
        b"        pass\n"
    )
    adapter = PythonParserAdapter()
    by_qname = {s.qualified_name: s for s in adapter.extract_symbols("m.py", source)}

    assert by_qname["Hidden"].kind == SymbolKind.class_
    assert by_qname["helper"].kind == SymbolKind.function
    assert by_qname["fallback"].kind == SymbolKind.function
    assert by_qname["Foo.bar"].kind == SymbolKind.method
    # Function-local nested defs stay out of scope (unchanged V1 rule).
    assert "outer.inner" not in by_qname
    assert "inner" not in by_qname


def test_typescript_qualified_name_for_a_method_includes_class_name() -> None:
    source = b"""
class Widget {
  render() { return 1; }
}
"""
    adapter = TypeScriptParserAdapter()
    tree = adapter._parser.parse(source)
    method_node = _find_node(tree.root_node, "method_definition")
    assert adapter.qualified_name(method_node) == "Widget.render"
