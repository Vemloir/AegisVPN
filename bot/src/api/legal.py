"""The legal documents, served to the website.

The bot publishes these same Markdown files to Telegraph; the site renders them
in place. Both read from src/privacy/, so there is one copy of the text and a
version bump lands everywhere at once.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from src.core.terms import TERMS_VERSION
from src.handlers.user.terms import load_privacy_text, load_tos_text

router = APIRouter()


@router.get("/api/legal/{doc}")
async def legal(doc: str, response: Response, lang: str = "ru") -> dict | None:
    if doc == "privacy":
        text = load_privacy_text(lang)
    elif doc == "tos":
        # The ToS exists in Russian only (the bot serves it that way too).
        text = load_tos_text()
    else:
        response.status_code = 404
        return None
    return {"version": TERMS_VERSION, "text": text}
