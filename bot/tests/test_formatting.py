from src.handlers.admin.common import fmt_bytes


def test_fmt_bytes_zero_and_none():
    assert fmt_bytes(0) == "0 B"
    assert fmt_bytes(None) == "0 B"
    assert fmt_bytes(-5) == "0 B"


def test_fmt_bytes_units():
    assert fmt_bytes(512) == "512 B"
    assert fmt_bytes(1024) == "1.0 KB"
    assert fmt_bytes(1536) == "1.5 KB"
    assert fmt_bytes(1024**3) == "1.0 GB"


def test_fmt_bytes_drops_decimal_when_large():
    assert fmt_bytes(200 * 1024**2) == "200 MB"
