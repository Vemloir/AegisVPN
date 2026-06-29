from src.pagination import Page


def test_single_page():
    p = Page(page=0, per_page=5, total=3)
    assert p.total_pages == 1
    assert (p.offset, p.has_prev, p.has_next) == (0, False, False)


def test_empty():
    p = Page(page=0, per_page=5, total=0)
    assert p.total_pages == 1
    assert p.offset == 0 and not p.has_prev and not p.has_next


def test_exact_multiple():
    p = Page(page=0, per_page=5, total=10)
    assert p.total_pages == 2
    assert p.has_next and not p.has_prev


def test_middle_page():
    p = Page(page=1, per_page=5, total=12)
    assert p.total_pages == 3
    assert p.offset == 5 and p.has_prev and p.has_next


def test_last_page_partial():
    p = Page(page=2, per_page=5, total=12)
    assert p.offset == 10 and p.has_prev and not p.has_next


def test_clamp_overshoot():
    p = Page(page=99, per_page=5, total=12)
    assert p.clamped == 2 and p.offset == 10 and not p.has_next


def test_clamp_negative():
    p = Page(page=-3, per_page=5, total=12)
    assert p.clamped == 0 and p.offset == 0 and not p.has_prev
