"""Acceptance gate keyboard: two document URL buttons + one accept button, with
graceful fallback when a Telegraph URL fails to resolve."""

import src.handlers.user.terms as terms
from src.handlers.user.terms import ACCEPT_CALLBACK, gate_keyboard


async def test_gate_keyboard_has_two_doc_buttons_and_accept(monkeypatch):
    async def _privacy(language):
        return "https://telegra.ph/privacy"

    async def _tos():
        return "https://telegra.ph/tos"

    monkeypatch.setattr(terms, "resolve_privacy_url", _privacy)
    monkeypatch.setattr(terms, "resolve_tos_url", _tos)

    kb = await gate_keyboard("ru")
    rows = kb.inline_keyboard
    # Row 1: two URL buttons (privacy + tos). Row 2: accept callback button.
    assert len(rows) == 2
    assert len(rows[0]) == 2
    assert rows[0][0].url == "https://telegra.ph/privacy"
    assert rows[0][1].url == "https://telegra.ph/tos"
    assert len(rows[1]) == 1
    assert rows[1][0].callback_data == ACCEPT_CALLBACK
    assert rows[1][0].url is None


async def test_gate_keyboard_omits_unresolvable_doc_button(monkeypatch):
    async def _privacy(language):
        return None  # publishing failed

    async def _tos():
        return "https://telegra.ph/tos"

    monkeypatch.setattr(terms, "resolve_privacy_url", _privacy)
    monkeypatch.setattr(terms, "resolve_tos_url", _tos)

    kb = await gate_keyboard("ru")
    rows = kb.inline_keyboard
    # Privacy omitted -> doc row has a single button; accept always present.
    assert len(rows) == 2
    assert len(rows[0]) == 1
    assert rows[0][0].url == "https://telegra.ph/tos"
    assert rows[1][0].callback_data == ACCEPT_CALLBACK


async def test_gate_keyboard_never_crashes_without_any_docs(monkeypatch):
    async def _none_privacy(language):
        return None

    async def _none_tos():
        return None

    monkeypatch.setattr(terms, "resolve_privacy_url", _none_privacy)
    monkeypatch.setattr(terms, "resolve_tos_url", _none_tos)

    kb = await gate_keyboard("ru")
    rows = kb.inline_keyboard
    # No doc buttons at all -> only the accept row, so the gate never deadlocks.
    assert len(rows) == 1
    assert rows[0][0].callback_data == ACCEPT_CALLBACK
