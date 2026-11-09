import sqlite3

from deploy.vps import register_external_server as register


def test_registration_can_stage_a_location_hidden_then_publish_it(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
            CREATE TABLE servers (
                id INTEGER PRIMARY KEY, name TEXT, flag TEXT, host TEXT, port INTEGER,
                public_key TEXT, short_id TEXT, agent_url TEXT, agent_token TEXT,
                access_mode TEXT, subscription_group TEXT, is_active INTEGER,
                tcp_port INTEGER, country_code TEXT, display_order INTEGER,
                control_mode TEXT, control_token_hash TEXT, control_cert_fingerprint TEXT,
                created_at TEXT, hidden_from_subscription INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE subscriptions (
                id INTEGER PRIMARY KEY, user_id INTEGER, client_uuid TEXT, is_active INTEGER
            );
        """)
    monkeypatch.setattr(register, "DB_PATH", db)
    values = {
        "SERVER_NAME": "Germany | Frankfurt", "SERVER_FLAG": "DE",
        "SERVER_HOST": "192.0.2.10", "SERVER_PORT": "443",
        "PUBLIC_KEY": "test-public-key", "SHORT_ID": "test-short-id",
        "AGENT_URL": "http://192.0.2.10:8444", "AGENT_TOKEN": "test-only-token",
        "HIDDEN_FROM_SUBSCRIPTION": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    register.main()
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT id,hidden_from_subscription FROM servers").fetchone()
    assert row[1] == 1
    monkeypatch.delenv("HIDDEN_FROM_SUBSCRIPTION")
    register.main()
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT id,hidden_from_subscription FROM servers").fetchall() == [(row[0], 0)]
