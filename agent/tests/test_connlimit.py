import pytest

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


# --- combined xray + Hy2 enforcement -----------------------------------------


def _patch_enforce(
    monkeypatch,
    *,
    xray_users,
    xray_ips,
    hy2_counts,
    limit=5,
):
    """Wire enforce_conn_limit_once to in-memory state and capture sib + Hy2
    kick calls. Returns (blocked_ips_list, hy2_kicked_list)."""
    monkeypatch.setattr(connlimit.settings, "conn_limit", limit)
    connlimit._overrides.clear()
    connlimit._prev_had_excess = False

    async def fake_online_users():
        return list(xray_users)

    async def fake_online_ips(email):
        return xray_ips.get(email, {})

    async def fake_hy2_counts():
        return dict(hy2_counts)

    kicked: list[list[str]] = []

    async def fake_kick(ids):
        kicked.append(list(ids))
        return True

    blocked: list[list[str]] = []

    async def fake_run_xray_api(args):
        # The sib invocation carries the excess IPs after the flags.
        ips = [a for a in args if "." in a and not a.startswith("--")]
        blocked.append(ips)
        return 0, ""

    monkeypatch.setattr(connlimit, "online_users", fake_online_users)
    monkeypatch.setattr(connlimit, "online_ips", fake_online_ips)
    monkeypatch.setattr(connlimit.hysteria, "online_counts", fake_hy2_counts)
    monkeypatch.setattr(connlimit.hysteria, "kick", fake_kick)
    monkeypatch.setattr(connlimit, "run_xray_api", fake_run_xray_api)
    return blocked, kicked


async def test_hy2_counts_toward_combined_limit(monkeypatch):
    # limit 5: 3 xray IPs + 3 Hy2 sessions = 6 > 5. xray is capped to
    # max(0, 5-3)=2 newest IPs, so the oldest 1 xray IP is blocked AND Hy2 is
    # kicked (it's contributing to the overage).
    blocked, kicked = _patch_enforce(
        monkeypatch,
        xray_users=["user_1_sub_1"],
        xray_ips={"user_1_sub_1": {"10.0.0.1": 100, "10.0.0.2": 200, "10.0.0.3": 300}},
        hy2_counts={"user_1_sub_1": 3},
        limit=5,
    )
    n = await connlimit.enforce_conn_limit_once()
    # Oldest xray IP (10.0.0.1, ts 100) blocked; the 2 newest kept.
    assert blocked[-1] == ["10.0.0.1"]
    assert n == 1
    assert kicked == [["user_1_sub_1"]]


async def test_hy2_only_user_over_limit_is_kicked(monkeypatch):
    # No xray session, 6 Hy2 sessions, limit 5 -> over -> Hy2 kicked, no xray IPs.
    blocked, kicked = _patch_enforce(
        monkeypatch,
        xray_users=[],
        xray_ips={},
        hy2_counts={"user_2_sub_2": 6},
        limit=5,
    )
    await connlimit.enforce_conn_limit_once()
    assert kicked == [["user_2_sub_2"]]
    # No xray IPs to block; sib runs with an empty set only if prev had excess.
    assert blocked == [] or blocked[-1] == []


async def test_combined_within_limit_no_action(monkeypatch):
    # 2 xray IPs + 2 Hy2 sessions = 4 <= 5 -> nothing blocked, nobody kicked.
    blocked, kicked = _patch_enforce(
        monkeypatch,
        xray_users=["user_3_sub_3"],
        xray_ips={"user_3_sub_3": {"10.0.0.1": 100, "10.0.0.2": 200}},
        hy2_counts={"user_3_sub_3": 2},
        limit=5,
    )
    n = await connlimit.enforce_conn_limit_once()
    assert n == 0
    assert kicked == []  # Hy2 not touched when under the limit
    assert blocked == []  # sib skipped entirely (no excess, no prior excess)


async def test_unlimited_user_never_kicked_or_blocked(monkeypatch):
    blocked, kicked = _patch_enforce(
        monkeypatch,
        xray_users=["user_4_sub_4"],
        xray_ips={"user_4_sub_4": {"10.0.0.1": 1, "10.0.0.2": 2, "10.0.0.3": 3}},
        hy2_counts={"user_4_sub_4": 4},
        limit=5,
    )
    connlimit.set_override(4, 0)  # 0 = unlimited
    await connlimit.enforce_conn_limit_once()
    assert kicked == []
    assert blocked == []


@pytest.mark.parametrize("data", [b'{"user_1_sub_1": 2, "u2": 1}', b'["user_1_sub_1"]'])
async def test_hy2_online_counts_shape(monkeypatch, data):
    from app import hysteria

    monkeypatch.setattr(hysteria.settings, "hy2_enabled", True)
    monkeypatch.setattr(hysteria, "_request", lambda *a, **k: data)
    counts = await hysteria.online_counts()
    assert counts.get("user_1_sub_1", 0) >= 1
