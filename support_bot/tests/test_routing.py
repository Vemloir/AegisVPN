from src.routing import Action, decide, operator_header

ADMINS = [111, 222]


def test_user_message_forwards_to_operators():
    assert decide(is_start_command=False, is_private_chat=True, from_user_id=999, admin_ids=ADMINS, is_reply=False) is Action.FORWARD_TO_OPERATORS


def test_start_greets_even_for_user():
    assert decide(is_start_command=True, is_private_chat=True, from_user_id=999, admin_ids=ADMINS, is_reply=False) is Action.WELCOME


def test_operator_reply_relays_to_user():
    assert decide(is_start_command=False, is_private_chat=True, from_user_id=111, admin_ids=ADMINS, is_reply=True) is Action.RELAY_TO_USER


def test_operator_without_reply_gets_hint():
    assert decide(is_start_command=False, is_private_chat=True, from_user_id=111, admin_ids=ADMINS, is_reply=False) is Action.OPERATOR_REPLY_HINT


def test_operator_start_greets_not_relays():
    # /start from an operator should greet, not be treated as a (non-)reply ticket
    assert decide(is_start_command=True, is_private_chat=True, from_user_id=222, admin_ids=ADMINS, is_reply=False) is Action.WELCOME


def test_non_private_is_ignored():
    assert decide(is_start_command=False, is_private_chat=False, from_user_id=999, admin_ids=ADMINS, is_reply=False) is Action.IGNORE
    assert decide(is_start_command=True, is_private_chat=False, from_user_id=111, admin_ids=ADMINS, is_reply=True) is Action.IGNORE


def test_missing_user_is_ignored():
    assert decide(is_start_command=False, is_private_chat=True, from_user_id=None, admin_ids=ADMINS, is_reply=False) is Action.IGNORE


def test_header_with_and_without_username():
    assert operator_header(full_name="Иван", user_id=7, username="ivan") == "Сообщение от Иван (ID: 7, @ivan):"
    assert operator_header(full_name="Ann Lee", user_id=8, username=None) == "Сообщение от Ann Lee (ID: 8):"
