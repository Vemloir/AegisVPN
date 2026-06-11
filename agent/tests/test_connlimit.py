from app import connlimit


def test_user_id_from_email():
    assert connlimit._user_id_from_email("user_42_sub_7") == 42
    assert connlimit._user_id_from_email("user_42_sub_7_dev_3") == 42
    assert connlimit._user_id_from_email("garbage") is None
    assert connlimit._user_id_from_email("user_x_sub_1") is None


def test_override_set_and_limit_for(monkeypatch, tmp_path):
    monkeypatch.setattr(connlimit, "_OVERRIDES_PATH", str(tmp_path / "cl.json"))
    monkeypatch.setattr(connlimit.settings, "conn_limit", 5)
    connlimit._overrides.clear()

    # default falls back to node setting
    assert connlimit._limit_for("user_1_sub_1") == 5
    # per-user override
    connlimit.set_override(1, 2)
    assert connlimit._limit_for("user_1_sub_1") == 2
    assert connlimit._limit_for("user_1_sub_1_dev_9") == 2  # devices share the user's limit
    # unlimited
    connlimit.set_override(1, 0)
    assert connlimit._limit_for("user_1_sub_1") == 0
    # clear → back to default
    connlimit.set_override(1, None)
    assert connlimit._limit_for("user_1_sub_1") == 5
