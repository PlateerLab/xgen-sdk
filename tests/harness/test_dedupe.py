"""memory/dedupe — content_fingerprint + dedupe 일반 유틸."""

from xgen_sdk.harness.memory.dedupe import content_fingerprint, dedupe


def test_fingerprint_normalizes_whitespace_and_case():
    a = content_fingerprint("Hello   World")
    b = content_fingerprint("hello world")
    c = content_fingerprint("  HELLO\nWORLD  ")
    assert a == b == c


def test_fingerprint_distinguishes_different_content():
    assert content_fingerprint("apple") != content_fingerprint("orange")


def test_fingerprint_raw_mode_keeps_case_and_space():
    assert content_fingerprint("A B", normalize=False) != content_fingerprint("a b", normalize=False)


def test_dedupe_preserves_order_first_wins():
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]


def test_dedupe_with_key_by_fingerprint():
    items = ["Hello World", "hello   world", "Other"]
    out = dedupe(items, key=content_fingerprint)
    assert out == ["Hello World", "Other"]


def test_dedupe_empty():
    assert dedupe([]) == []
