from __future__ import annotations

from rune.core.hashing import content_hash, working_tree_fingerprint


def test_content_hash_stable_for_same_bytes() -> None:
    assert content_hash(b"hello") == content_hash(b"hello")


def test_content_hash_differs_for_different_bytes() -> None:
    assert content_hash(b"hello") != content_hash(b"world")


def test_content_hash_handles_unicode_and_crlf_bytes() -> None:
    # Windows compatibility: CRLF line endings and non-ASCII (中文) content
    # must hash deterministically like any other bytes.
    a = "第一行\r\n第二行\r\n".encode()
    b = "第一行\r\n第二行\r\n".encode()
    assert content_hash(a) == content_hash(b)


def test_working_tree_fingerprint_is_order_independent() -> None:
    a = {"b.py": "sha256:2", "a.py": "sha256:1"}
    b = {"a.py": "sha256:1", "b.py": "sha256:2"}
    assert working_tree_fingerprint(a) == working_tree_fingerprint(b)


def test_working_tree_fingerprint_changes_when_content_changes() -> None:
    a = {"a.py": "sha256:1"}
    b = {"a.py": "sha256:2"}
    assert working_tree_fingerprint(a) != working_tree_fingerprint(b)


def test_working_tree_fingerprint_empty_is_deterministic() -> None:
    assert working_tree_fingerprint({}) == working_tree_fingerprint({})
