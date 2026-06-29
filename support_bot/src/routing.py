"""Pure routing decision for an incoming message — no aiogram, fully unit-testable.

The whole bot is a two-way relay, so every message reduces to one of these:
- a user wrote in -> fan the message out to the operators;
- an operator replied to a forwarded ticket -> relay it back to the user;
- an operator wrote without replying -> tell them to reply to a ticket;
- /start -> greet;
- anything outside a private chat -> ignore.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Action(StrEnum):
    WELCOME = "welcome"
    FORWARD_TO_OPERATORS = "forward_to_operators"
    RELAY_TO_USER = "relay_to_user"
    OPERATOR_REPLY_HINT = "operator_reply_hint"
    IGNORE = "ignore"


def decide(
    *,
    is_start_command: bool,
    is_private_chat: bool,
    from_user_id: int | None,
    admin_ids: Iterable[int],
    is_reply: bool,
) -> Action:
    if not is_private_chat or from_user_id is None:
        return Action.IGNORE
    if is_start_command:
        return Action.WELCOME
    if from_user_id in set(admin_ids):
        return Action.RELAY_TO_USER if is_reply else Action.OPERATOR_REPLY_HINT
    return Action.FORWARD_TO_OPERATORS


def operator_header(*, full_name: str, user_id: int, username: str | None) -> str:
    """Plain-text (no HTML, no emoji) banner shown above a forwarded ticket so the
    operator sees who wrote, even if the user hid their forward origin."""
    handle = f", @{username}" if username else ""
    return f"Сообщение от {full_name} (ID: {user_id}{handle}):"
