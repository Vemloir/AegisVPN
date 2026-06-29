"""Pure pagination math for the 'My tickets' list — no aiogram, fully testable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    page: int  # requested 0-based page (may be out of range; use .clamped)
    per_page: int
    total: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.per_page - 1) // self.per_page)

    @property
    def clamped(self) -> int:
        return min(max(self.page, 0), self.total_pages - 1)

    @property
    def offset(self) -> int:
        return self.clamped * self.per_page

    @property
    def has_prev(self) -> bool:
        return self.clamped > 0

    @property
    def has_next(self) -> bool:
        return self.clamped < self.total_pages - 1
