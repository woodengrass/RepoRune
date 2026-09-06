from __future__ import annotations

from rune.core.index.treesitter import (
    JavaScriptParserAdapter,
    PythonParserAdapter,
    TypeScriptParserAdapter,
    get_parser_adapter,
    symbol_id,
)
from rune.core.storage.models import SymbolKind

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
