"""Push code updates to running AegisVPN nodes.

Two independent targets:
  --bot   : upload changed bot sources to the main VPS and recreate the bot
            container WITHOUT touching aegis-vpn (avoids the depends_on restart).
  --nodes : upload updated agent sources to one or more VPN nodes and restart
            only the vpn container there.

Both targets can be combined in one run.

Usage examples:

  # Update the bot only (main VPS):
  python update.py --main-host MAIN_IP --main-password '…' --bot

  # Update agent on two nodes:
  python update.py --node NODE_IP:ROOT_PASSWORD \\
                   --node NODE_IP:ROOT_PASSWORD \\
                   --nodes

  # Both at once:
  python update.py --main-host MAIN_IP --main-password '…' --bot \\
                   --node NODE_IP:ROOT_PASSWORD --nodes

  # Switch nodes' Xray transport (xhttp|tcp) and mirror reality keys into the
  # bot DB in one atomic pass (needs --main-password for the DB write):
  python update.py --set-network xhttp --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD

  # Roll the xhttp tuning (server mode=auto, connIdle=60, tcpKeepAlive) onto
  # already-xhttp nodes WITHOUT breaking the live subscriptions: push the new
  # agent (brings the connIdle/sockopt entrypoint logic) AND re-sync the env via
  # --set-network xhttp (rewrites XHTTP_MODE=auto). auto keeps existing packet-up
  # clients working — no re-import. Combine both in one invocation:
  python update.py --nodes --set-network xhttp \\
                   --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD

  # One-time: migrate nodes to the split xray+agent topology so future code
  # deploys (--nodes) recreate ONLY the agent and drop no client sessions:
  python update.py --split-migrate \\
                   --node NODE_IP:ROOT_PASSWORD --node NODE_IP:ROOT_PASSWORD

  # Roll the full Greece-canary stack (xray raw-TCP+VISION :2053 + Hysteria2)
  # onto a node and sync its bot DB row. Idempotent + safe to re-run. The
  # geo-SNI (Hy2 cert CN) comes from the built-in IP map, or pass --geo-sni:
  python update.py --provision-stack \\
                   --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --geo-sni csc.fi
"""

from __future__ import annotations

import argparse
import posixpath
import stat
import time
from pathlib import Path

try:
    import paramiko
except ImportError as exc:
    raise SystemExit("paramiko required: pip install paramiko") from exc

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
BOT_DIR = ROOT / "bot"


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def connect(host: str, password: str, attempts: int = 4) -> paramiko.SSHClient:
    delay = 8.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(host, username="root", password=password,
                      timeout=30, banner_timeout=30, auth_timeout=30)
            return c
        except Exception as exc:
            last = exc
            try: c.close()
            except Exception: pass
            if attempt == attempts:
                break
            print(f"  ssh retry {attempt}/{attempts-1} ({exc}) sleep {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.7, 45.0)
    raise SystemExit(f"cannot reach {host}: {last}")


def run(c: paramiko.SSHClient, cmd: str, label: str = "", timeout: int = 120) -> str:
    _, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        raise SystemExit(f"{label or cmd} failed (exit {rc}):\n{(err or out).strip()[-800:]}")
    return out


def get_sftp(c: paramiko.SSHClient) -> paramiko.SFTPClient:
    sftp = getattr(c, "_sftp", None)
    if sftp is None:
        sftp = c.open_sftp()
        c._sftp = sftp  # type: ignore[attr-defined]
    return sftp


def upload(c: paramiko.SSHClient, local: Path, remote: str) -> None:
    sftp = get_sftp(c)
    remote_dir = posixpath.dirname(remote)
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        run(c, f"mkdir -p {remote_dir}")
    sftp.put(str(local), remote)
    if local.stat().st_mode & stat.S_IXUSR:
        sftp.chmod(remote, 0o755)


# ---------------------------------------------------------------------------
# Bot update
# ---------------------------------------------------------------------------

def update_bot(c: paramiko.SSHClient) -> None:
    """Upload all bot sources (code + assets) and recreate the bot container only."""
    print("  uploading bot sources…")
    # Upload EVERY source asset, not just .py — the privacy/ToS markdown (and any
    # future templates/json) live under src/ too and must reach the image. A
    # .py-only sweep silently shipped stale .md files and dropped new assets.
    for f in sorted((BOT_DIR / "src").rglob("*")):
        if f.is_dir() or "__pycache__" in f.parts or f.suffix == ".pyc":
            continue
        rel = f.relative_to(BOT_DIR)
        upload(c, f, f"/root/aegis/bot/{rel.as_posix()}")

    # Dependency manifests must ship too — the image installs from pyproject.toml,
    # so a dependency change is invisible to the build unless these are uploaded.
    for manifest in ("pyproject.toml", "uv.lock"):
        src = BOT_DIR / manifest
        if src.exists():
            upload(c, src, f"/root/aegis/bot/{manifest}")

    # Recreate bot container WITHOUT going through docker compose up
    # (which would also restart aegis-vpn via depends_on).
    print("  rebuilding image…")
    run(c, "cd /root/aegis/deploy/vps && docker compose build bot 2>&1 | tail -3",
        "docker build bot", timeout=180)

    print("  recreating container (vpn untouched)…")
    run(c, "docker stop aegis-bot 2>/dev/null || true && docker rm aegis-bot 2>/dev/null || true",
        "remove old bot")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps bot 2>&1",
        "start bot", timeout=60)
    print("  bot updated ✓")


# ---------------------------------------------------------------------------
# Agent (node) update
# ---------------------------------------------------------------------------

COMPOSE_LOCAL = ROOT / "deploy/vps/docker-compose.yml"
REMOTE_COMPOSE = "/root/aegis/deploy/vps/docker-compose.yml"


def _upload_agent_sources(c: paramiko.SSHClient, host: str) -> None:
    print(f"  [{host}] uploading agent sources…")
    base_files = [
        AGENT_DIR / "Dockerfile",
        AGENT_DIR / "entrypoint.sh",
        AGENT_DIR / "pyproject.toml",
        AGENT_DIR / "uv.lock",
        AGENT_DIR / "template.json",
    ]
    for f in base_files + sorted((AGENT_DIR / "app").glob("*.py")):
        rel = f.relative_to(AGENT_DIR)
        upload(c, f, f"/root/aegis/agent/{rel.as_posix()}")


def update_agent(c: paramiko.SSHClient, host: str) -> None:
    """Upload agent sources and recreate ONLY the agent container.

    In the split topology xray runs in its own container, so rebuilding `agent`
    leaves the data plane (and every live client session) untouched — code
    deploys are zero-drop. Run --split-migrate once first to create the split.
    """
    _upload_agent_sources(c, host)
    print(f"  [{host}] rebuilding + recreating agent (xray untouched)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --build --no-deps agent 2>&1 | tail -4",
        "agent rebuild", timeout=300)
    print(f"  [{host}] agent updated ✓")


def split_migrate(c: paramiko.SSHClient, host: str) -> None:
    """One-time: migrate a node from the single `vpn` container to the split
    `xray` + `agent` topology. Costs one brief xray blip at cutover; afterwards
    `--nodes` recreates only `agent`, so future code deploys drop no sessions.
    """
    _upload_agent_sources(c, host)
    print(f"  [{host}] uploading docker-compose.yml…")
    upload(c, COMPOSE_LOCAL, REMOTE_COMPOSE)
    print(f"  [{host}] building split images (old container still serving)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose build xray agent 2>&1 | tail -4",
        "split build", timeout=400)
    print(f"  [{host}] cutover: dropping single container, bringing up xray+agent…")
    # The old `vpn` service holds the aegis-vpn name the new `agent` service
    # wants; remove it (also stops the bundled xray) then start the split. xray
    # reads the existing config straight off the shared ./data/vpn volume.
    run(c, "docker rm -f aegis-vpn 2>/dev/null || true", "rm old vpn")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps xray agent 2>&1 | tail -6",
        "split up", timeout=120)
    out = run(c, "sleep 5; docker ps --filter name=aegis-xray --filter name=aegis-vpn "
                 "--format '{{.Names}} {{.Status}}'", "verify split", timeout=60)
    print(f"  [{host}] live containers:\n    " + "\n    ".join(out.strip().splitlines() or ["(none!)"]))


# ---------------------------------------------------------------------------
# Transport switch (xhttp <-> tcp)
# ---------------------------------------------------------------------------

# agent.env lives on the host via the vpn service's ./data/vpn:/data volume.
REMOTE_AGENT_ENV = "/root/aegis/deploy/vps/data/vpn/agent.env"


def _parse_env(blob: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _render_env(env: dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def set_network(
    c: paramiko.SSHClient, host: str, network: str, xhttp_mode: str = "auto"
) -> tuple[str, str]:
    """Flip XRAY_NETWORK in the node's agent.env and restart the vpn container.

    Returns (public_key, short_id) matching the *target* transport. Reality uses
    a different keypair per transport, and the bot overrides pbk/sid from its DB
    in normalize_vless_uri — so these MUST be mirrored into the DB or the reality
    handshake breaks. Clients are preserved: entrypoint rebuilds the inbound from
    the same on-disk config, only adding/stripping the vision flow as needed.

    For xhttp, the server mode is forced to `xhttp_mode` (default "auto"): auto
    accepts existing packet-up clients AND new stream-up/stream-one ones, so the
    15 live subscriptions keep working with no re-import. An explicit non-auto
    mode would 400 every mismatched client — don't pass one unless re-issuing.
    """
    sftp = get_sftp(c)
    with sftp.open(REMOTE_AGENT_ENV, "r") as fh:
        env = _parse_env(fh.read().decode())

    current = (env.get("XRAY_NETWORK") or "tcp").lower()
    if network == "xhttp":
        pubkey, sid = env.get("PUBLIC_KEY"), env.get("SHORT_ID")
        env.setdefault("XHTTP_PATH", "/")
        env["XHTTP_MODE"] = xhttp_mode
        env.setdefault("XRAY_CONN_IDLE", "60")
    else:
        pubkey = env.get("PUBLIC_KEY_TCP") or env.get("PUBLIC_KEY")
        sid = env.get("SHORT_ID_TCP") or env.get("SHORT_ID")
    if not pubkey or not sid:
        raise SystemExit(
            f"[{host}] agent.env missing reality keys for {network} "
            f"(pubkey={bool(pubkey)}, sid={bool(sid)}) — aborting"
        )

    if current == network:
        print(f"  [{host}] XRAY_NETWORK already '{network}' — re-syncing keys anyway")
    env["XRAY_NETWORK"] = network
    print(f"  [{host}] XRAY_NETWORK {current} -> {network}")
    if network == "xhttp":
        print(f"  [{host}] XHTTP_MODE -> {xhttp_mode}, connIdle -> {env['XRAY_CONN_IDLE']}s")
    with sftp.open(REMOTE_AGENT_ENV, "w") as fh:
        fh.write(_render_env(env))

    # Split topology only (run --split-migrate first): recreate the agent so it
    # rebuilds the config from the new env, then restart xray to load it. A
    # transport change can't be hot-applied, so this xray bounce drops sessions
    # once — unavoidable for a mode/keypair change.
    print(f"  [{host}] applying: recreate agent + reload xray…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --no-deps --force-recreate agent 2>&1 | tail -3",
        "agent recreate", timeout=180)
    run(c, "cd /root/aegis/deploy/vps && docker compose restart xray 2>&1 | tail -2",
        "xray reload", timeout=120)
    out = run(
        c,
        "sleep 5; docker exec aegis-vpn sh -c "
        "'grep -m1 -o \"\\\"network\\\": \\\"[a-z]*\\\"\" "
        "\"${XRAY_CONFIG_PATH:-/etc/xray/config.json}\"' || true",
        "verify network", timeout=60,
    )
    print(f"  [{host}] live inbound: {out.strip() or '(unverified)'}")
    return pubkey, sid


def set_db_keys(main_c: paramiko.SSHClient, host: str, pubkey: str, sid: str) -> None:
    """Mirror a node's active reality pubkey/short_id into its bot DB row."""
    py = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.core.database import async_session_maker\n"
        f"PK, SID, HOST = {pubkey!r}, {sid!r}, {host!r}\n"
        "async def q():\n"
        "    async with async_session_maker() as s:\n"
        "        res = await s.execute(text('UPDATE servers SET public_key=:pk, short_id=:sid WHERE host=:h'),"
        " {'pk': PK, 'sid': SID, 'h': HOST})\n"
        "        await s.commit()\n"
        "        print('rows updated:', res.rowcount)\n"
        "        r = await s.execute(text('SELECT id, name, host, public_key, short_id FROM servers WHERE host=:h'),"
        " {'h': HOST})\n"
        "        for row in r.fetchall(): print('  now:', tuple(row))\n"
        "asyncio.run(q())\n"
    )
    cmd = "docker exec -i aegis-bot python3 <<'PYEOF'\n" + py + "PYEOF\n"
    out = run(main_c, cmd, f"db update {host}", timeout=60)
    print(out.rstrip())
    if "rows updated: 0" in out:
        raise SystemExit(f"[{host}] no servers row matched host={host} — DB NOT updated")


# ---------------------------------------------------------------------------
# Full-stack provisioning (--provision-stack)
# ---------------------------------------------------------------------------
#
# Encodes the steps that were done by hand on the Greece canary (45.142.31.13)
# so the same multi-inbound (xray tcp+VISION) + Hysteria2 stack rolls onto the
# other nodes with one command. Every step is IDEMPOTENT and COMPOSABLE:
#
#   provision_agent_env(c, host, geo_sni)  -> A) per-node agent.env knobs
#   provision_hysteria(c, host, geo_sni)   -> B) cert + secrets + config + ports
#   provision_code(c, host)                -> C) agent sources + recreate + xray
#   provision_mtproxy(c, host, ...)        -> (stub) future Telegram MTProto proxy
#   sync_bot_db_hy2(main_c, host, obfs)    -> D) mirror the row into the bot DB
#
# provision_stack() wires them together and verifies at the end. Re-running is
# safe: the cert/secrets/iptables rule are only created when absent, and the
# existing data/hysteria/config.yaml's secrets are REUSED so the bot DB stays in
# sync with whatever the node already serves.

# Per-node geo-SNI / self-signed cert CN. The REALITY_SERVER_NAME/REALITY_DEST
# are set by a SEPARATE step (--add-server / setup) and are NOT touched here;
# this map only drives the Hysteria2 self-signed cert CN (cosmetic — Hy2 clients
# set insecure=1, but a plausible CN keeps a passive probe boring). Override per
# run with --geo-sni if a node's value differs.
NODE_GEO_SNI = {
    # Finland
    "csc.fi": "csc.fi",
    # Sweden / Norway / Greece / Japan — keyed by IP below where known.
}
NODE_IP_GEO_SNI = {
    "45.142.31.13": "aegean.gr",          # Greece (the canary)
    # Fill in the others as they roll out (or pass --geo-sni):
    #   Finland  -> csc.fi
    #   Sweden   -> www.chalmers.se
    #   Norway   -> uio.no
    #   Japan    -> www.osaka-u.ac.jp
}

# --- Port plan -------------------------------------------------------------
# Reserve a stable, documented port layout so services never collide and a
# future Telegram (MTProto) proxy slots in cleanly:
#
#   443         xray primary inbound      (XRAY_PORT, current network)
#   2053        xray raw-TCP + VISION     (XRAY_TCP_PORT)
#   8443        Caddy HTTPS (subscription)
#   8444        agent API (loopback)      hy2 auth callback :8444/hy2/auth
#   9999        Hy2 trafficStats          (loopback)
#   36500       Hysteria2 UDP listen
#   20000-50000 Hy2 UDP port-hop range -> REDIRECT to 36500
#   --- reserved for the future MTProto proxy (mtg, `mtproxy` compose profile) ---
#   8765        mtg MTProto listen        (RESERVED — see provision_mtproxy)
#
XRAY_TCP_PORT = 2053
XRAY_CONN_IDLE = 30
HY2_LISTEN_PORT = 36500
HY2_HOP_START = 20000
HY2_HOP_END = 50000
HY2_BW_UP = "100 mbps"      # honest fixed Brutal rate (the tuned Greece value)
HY2_BW_DOWN = "100 mbps"
HY2_STATS_URL = "http://127.0.0.1:9999"
MTPROXY_PORT = 8765         # RESERVED for the future mtg MTProto proxy

REMOTE_HY2_DIR = "/root/aegis/deploy/vps/data/hysteria"
REMOTE_HY2_CONFIG = f"{REMOTE_HY2_DIR}/config.yaml"
HY2_TEMPLATE_LOCAL = ROOT / "deploy/vps/hysteria/config.template.yaml"

# Hy2 TLS cert renewal (Let's Encrypt via acme.sh on the ACME host). The domain
# is never hardcoded here (public repo): the cert dir is discovered by globbing
# the single *_ecc directory acme.sh created.
REMOTE_ACME_DIR = "/root/acme"
ACME_IMAGE = "neilpang/acme.sh"

# Telegram MTProto proxy (fake-TLS mtg). The camouflage SNI is baked into the
# ee-secret; a reachable, unblocked big-CDN host is the goal. Overridable.
MTPROXY_IMAGE = "nineseconds/mtg:2"
MTPROXY_FAKE_TLS_DOMAIN = "www.cloudflare.com"


def _gen_secret(nchars: int = 32) -> str:
    """url-safe ~`nchars`-char secret, generated locally (never logged in full)."""
    import secrets
    return secrets.token_urlsafe(nchars)[:nchars]


def _remote_exists(c: paramiko.SSHClient, path: str) -> bool:
    sftp = get_sftp(c)
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def resolve_geo_sni(host: str, override: str | None) -> str:
    """Pick the cert CN / geo-SNI for a node: explicit flag wins, else the map."""
    if override:
        return override
    sni = NODE_IP_GEO_SNI.get(host)
    if not sni:
        raise SystemExit(
            f"[{host}] no geo-SNI known for this IP — pass --geo-sni <name> "
            f"(e.g. csc.fi / www.chalmers.se / uio.no / aegean.gr / "
            f"www.osaka-u.ac.jp)"
        )
    return sni


def provision_agent_env(
    c: paramiko.SSHClient, host: str, geo_sni: str | None = None
) -> str:
    """A) Set the per-node agent.env knobs for the multi-inbound + Hy2 stack.

    Idempotent: rewrites only the keys we own and PRESERVES the reality keypairs.
    When `geo_sni` is given it becomes the REALITY serverName/dest, but ONLY for an
    un-geo'd / fresh node (REALITY_SERVER_NAME empty or its first entry is exactly
    gateway.icloud.com) — an already-provisioned node keeps its existing
    REALITY_SERVER_NAME / REALITY_DEST untouched (incl. any icloud migration
    alias), so a re-run never disturbs live REALITY. Generates HY2_STATS_SECRET
    once and reuses it on re-run. Never sets a gRPC port (gRPC was dropped).
    Returns the HY2_STATS_SECRET so the same value flows into the Hy2 config.
    """
    sftp = get_sftp(c)
    if not _remote_exists(c, REMOTE_AGENT_ENV):
        raise SystemExit(
            f"[{host}] {REMOTE_AGENT_ENV} missing — provision the base node first "
            f"(add_server.py / the agent must have initialised its keys)"
        )
    with sftp.open(REMOTE_AGENT_ENV, "r") as fh:
        env = _parse_env(fh.read().decode())

    env["XRAY_TCP_PORT"] = str(XRAY_TCP_PORT)
    env["XRAY_CONN_IDLE"] = str(XRAY_CONN_IDLE)
    env["HY2_ENABLED"] = "true"
    env["HY2_STATS_URL"] = HY2_STATS_URL
    # Reuse the stats secret across re-runs so the rendered Hy2 config and the
    # agent.env never drift apart.
    existing_secret = env.get("HY2_STATS_SECRET")
    stats_secret = existing_secret or _gen_secret(32)
    env["HY2_STATS_SECRET"] = stats_secret
    # Defensively make sure no stale gRPC port lingers (gRPC is dropped).
    env.pop("XRAY_GRPC_PORT", None)
    env.pop("XRAY_GRPC_SERVICE", None)

    # Geo-SNI -> REALITY, but ONLY for an un-geo'd node (empty or the bare
    # gateway.icloud.com default). An already-geo'd node is left exactly as-is so
    # a re-run never rewrites live REALITY or strips a migration alias.
    if geo_sni:
        current = env.get("REALITY_SERVER_NAME", "").strip()
        first = current.split(",")[0].strip() if current else ""
        if not first or first == "gateway.icloud.com":
            env["REALITY_SERVER_NAME"] = geo_sni
            env["REALITY_DEST"] = f"{geo_sni}:443"
            print(f"  [{host}] agent.env: REALITY_SERVER_NAME={geo_sni} (set on fresh node)")
        else:
            print(f"  [{host}] agent.env: REALITY preserved (already geo'd: {first})")

    print(f"  [{host}] agent.env: XRAY_TCP_PORT={XRAY_TCP_PORT} "
          f"XRAY_CONN_IDLE={XRAY_CONN_IDLE} HY2_ENABLED=true "
          f"HY2_STATS_SECRET={'(reused)' if existing_secret else '(new)'}")
    with sftp.open(REMOTE_AGENT_ENV, "w") as fh:
        fh.write(_render_env(env))
    return stats_secret


def provision_hysteria(
    c: paramiko.SSHClient, host: str, geo_sni: str, stats_secret: str
) -> str:
    """B) Provision Hysteria2 on the node. Returns the OBFS_PASSWORD (for the DB).

    Idempotent end to end:
      * data/hysteria/{key,cert}.pem generated only if absent (CN = geo_sni).
      * OBFS_PASSWORD: REUSED from an existing data/hysteria/config.yaml if one
        is present, else freshly generated — so the bot DB stays in sync with
        whatever obfs the node actually serves.
      * config.yaml rendered from the repo template with the obfs password, the
        agent.env stats secret, and the 100/100 Brutal bandwidth.
      * iptables REDIRECT for the UDP hop range added only if not already there
        (-C guard) and persisted across reboot via a tiny systemd unit.
      * `docker compose --profile hysteria up -d hysteria`.
    """
    print(f"  [{host}] hysteria: ensuring {REMOTE_HY2_DIR}")
    run(c, f"mkdir -p {REMOTE_HY2_DIR}", "mkdir hysteria")

    # --- self-signed cert (idempotent) ---
    key_pem = f"{REMOTE_HY2_DIR}/key.pem"
    cert_pem = f"{REMOTE_HY2_DIR}/cert.pem"
    if _remote_exists(c, key_pem) and _remote_exists(c, cert_pem):
        print(f"  [{host}] hysteria: cert present, keeping it")
    else:
        print(f"  [{host}] hysteria: generating self-signed cert (CN={geo_sni})")
        run(c,
            f"cd {REMOTE_HY2_DIR} && "
            f"openssl ecparam -genkey -name prime256v1 -out key.pem && "
            f"openssl req -new -x509 -days 3650 -key key.pem -out cert.pem "
            f"-subj '/CN={geo_sni}'",
            "openssl cert", timeout=60)

    # --- obfs password: reuse from an existing config so the DB stays in sync ---
    obfs_password: str | None = None
    if _remote_exists(c, REMOTE_HY2_CONFIG):
        sftp = get_sftp(c)
        with sftp.open(REMOTE_HY2_CONFIG, "r") as fh:
            for line in fh.read().decode().splitlines():
                s = line.strip()
                if s.startswith("password:"):
                    obfs_password = s.split(":", 1)[1].strip()
                    break
        if obfs_password:
            print(f"  [{host}] hysteria: reusing existing obfs password")
    if not obfs_password:
        obfs_password = _gen_secret(32)
        print(f"  [{host}] hysteria: generated new obfs password")

    # --- render config.yaml from the repo template ---
    template = HY2_TEMPLATE_LOCAL.read_text(encoding="utf-8")
    rendered = (
        template
        .replace("__OBFS_PASSWORD__", obfs_password)
        .replace("__STATS_SECRET__", stats_secret)
        .replace("__BW_UP__", HY2_BW_UP)
        .replace("__BW_DOWN__", HY2_BW_DOWN)
    )
    leftovers = [tok for tok in ("__OBFS_PASSWORD__", "__STATS_SECRET__",
                                 "__BW_UP__", "__BW_DOWN__") if tok in rendered]
    if leftovers:
        raise SystemExit(f"[{host}] hysteria config still has placeholders: {leftovers}")
    print(f"  [{host}] hysteria: rendering config.yaml")
    sftp = get_sftp(c)
    with sftp.open(REMOTE_HY2_CONFIG, "w") as fh:
        fh.write(rendered)

    # --- port-hopping REDIRECT (idempotent + reboot-persistent) ---
    print(f"  [{host}] hysteria: ensuring UDP hop REDIRECT "
          f"{HY2_HOP_START}:{HY2_HOP_END} -> {HY2_LISTEN_PORT}")
    redirect_rule = (
        f"-p udp --dport {HY2_HOP_START}:{HY2_HOP_END} "
        f"-j REDIRECT --to-ports {HY2_LISTEN_PORT}"
    )
    run(c,
        f"iptables -t nat -C PREROUTING {redirect_rule} 2>/dev/null || "
        f"iptables -t nat -A PREROUTING {redirect_rule}",
        "iptables redirect", timeout=30)
    _persist_hop_redirect(c, host, redirect_rule)

    # --- start (or restart) the hysteria container ---
    print(f"  [{host}] hysteria: docker compose up -d hysteria")
    # No `| tail` here: a pipe masks docker-compose's exit code, so a failed
    # image pull would slip through unnoticed. Capture the real exit status.
    run(c,
        "cd /root/aegis/deploy/vps && "
        "docker compose --profile hysteria up -d hysteria 2>&1",
        "hysteria up", timeout=180)
    return obfs_password


def _persist_hop_redirect(c: paramiko.SSHClient, host: str, redirect_rule: str) -> None:
    """Make the UDP-hop REDIRECT survive a reboot.

    iptables rules in the nat table are lost on reboot. Rather than depend on
    iptables-persistent being installed, drop a tiny oneshot systemd unit that
    re-applies the exact (idempotent, -C-guarded) rule at boot. Writing the unit
    is itself idempotent — same content every time.
    """
    unit = (
        "[Unit]\n"
        "Description=AegisVPN Hysteria2 UDP port-hop REDIRECT\n"
        "After=network-online.target docker.service\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/bin/sh -c 'iptables -t nat -C PREROUTING {redirect_rule} "
        f"2>/dev/null || iptables -t nat -A PREROUTING {redirect_rule}'\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    sftp = get_sftp(c)
    with sftp.open("/etc/systemd/system/aegis-hy2-hop.service", "w") as fh:
        fh.write(unit)
    run(c, "systemctl daemon-reload && systemctl enable aegis-hy2-hop.service 2>&1 | tail -1",
        "enable hop unit", timeout=30)
    print(f"  [{host}] hysteria: hop REDIRECT persisted (aegis-hy2-hop.service)")


def provision_code(c: paramiko.SSHClient, host: str) -> None:
    """C) Upload latest agent sources + docker-compose.yml, recreate the agent
    (which rebuilds the multi-inbound config from the new agent.env) and restart
    xray so it loads the rebuilt config.
    """
    _upload_agent_sources(c, host)
    print(f"  [{host}] uploading docker-compose.yml…")
    upload(c, COMPOSE_LOCAL, REMOTE_COMPOSE)
    print(f"  [{host}] recreating agent (rebuilds config from agent.env)…")
    run(c, "cd /root/aegis/deploy/vps && "
           "docker compose up -d --build --no-deps --force-recreate agent 2>&1 | tail -4",
        "agent recreate", timeout=300)
    print(f"  [{host}] restarting xray to load the multi-inbound config…")
    run(c, "cd /root/aegis/deploy/vps && docker compose restart xray 2>&1 | tail -2",
        "xray reload", timeout=120)


def renew_hy2_cert_acme(main_c: paramiko.SSHClient) -> tuple[str, str, str]:
    """Run acme.sh --cron on the ACME host (renews the shared Hy2 Let's Encrypt
    cert if due, via the DuckDNS DNS-01 creds acme.sh saved at issuance), then
    return (cert_b64, key_b64, domain) of the current fullchain + key. Domain-
    agnostic: globs the single *_ecc cert dir so this (public) script never
    hardcodes the operator's DuckDNS domain; `domain` (the cert CN, used as the
    bot's hy2_sni) is that dir's basename minus the trailing ``_ecc``."""
    print("  [acme] acme.sh --cron (renew if due)…")
    run(main_c,
        f"docker run --rm -v {REMOTE_ACME_DIR}:/acme.sh {ACME_IMAGE} "
        f"--cron --home /acme.sh 2>&1 | tail -6",
        "acme cron", timeout=300)
    cdir = run(main_c, f"ls -d {REMOTE_ACME_DIR}/*_ecc 2>/dev/null | head -1",
               "find cert dir", timeout=30).strip()
    if not cdir:
        raise SystemExit(
            f"[acme] no *_ecc cert dir under {REMOTE_ACME_DIR} — was the cert issued?")
    domain = posixpath.basename(cdir)
    if domain.endswith("_ecc"):
        domain = domain[: -len("_ecc")]
    cert_b64 = run(main_c, f"base64 -w0 {cdir}/fullchain.cer", "read cert", timeout=30).strip()
    key_b64 = run(main_c, f"base64 -w0 {cdir}/*.key", "read key", timeout=30).strip()
    if not cert_b64 or not key_b64:
        raise SystemExit(f"[acme] cert/key missing under {cdir}")
    enddate = run(main_c, f"openssl x509 -in {cdir}/fullchain.cer -noout -enddate 2>/dev/null",
                  "cert enddate", timeout=30).strip()
    print(f"  [acme] current cert for {domain} {enddate}")
    return cert_b64, key_b64, domain


def install_hy2_cert(c: paramiko.SSHClient, host: str, cert_b64: str, key_b64: str) -> None:
    """Write the LE cert + key into the node's hysteria dir (verifying the key
    matches the cert) and restart hysteria so it serves the fresh cert."""
    run(c, f"echo {cert_b64} | base64 -d > {REMOTE_HY2_DIR}/cert.pem", "write cert", timeout=30)
    run(c, f"echo {key_b64} | base64 -d > {REMOTE_HY2_DIR}/key.pem && "
           f"chmod 600 {REMOTE_HY2_DIR}/key.pem", "write key", timeout=30)
    match = run(c,
        f"openssl x509 -in {REMOTE_HY2_DIR}/cert.pem -noout -pubkey > /tmp/_aegcp 2>/dev/null; "
        f"openssl ec -in {REMOTE_HY2_DIR}/key.pem -pubout > /tmp/_aegkp 2>/dev/null; "
        f"if diff -q /tmp/_aegcp /tmp/_aegkp >/dev/null 2>&1; then echo MATCH; else echo MISMATCH; fi; "
        f"rm -f /tmp/_aegcp /tmp/_aegkp", "cert/key match", timeout=30).strip()
    if "MATCH" not in match:
        raise SystemExit(f"[{host}] cert/key mismatch after install — aborting (NOT restarting)")
    run(c, "cd /root/aegis/deploy/vps && docker compose restart hysteria 2>&1 | tail -1",
        "restart hysteria", timeout=120)
    print(f"  [{host}] cert installed + hysteria restarted")


def provision_mtproxy(
    c: paramiko.SSHClient, host: str, main_c: paramiko.SSHClient, server_id: int,
    fake_tls_domain: str = MTPROXY_FAKE_TLS_DOMAIN,
) -> str:
    """Provision an mtg fake-TLS MTProto proxy on the node (reserved :8765) and
    mirror its secret + port into the bot DB row `server_id`. Idempotent: reuses
    the already-running container's secret so a re-run never rotates it (which
    would invalidate links already handed to users). Returns the ee-secret."""
    print(f"  [{host}] mtproxy: ensuring fake-TLS secret…")
    secret = run(c,
        "docker inspect aegis-mtg --format '{{range .Args}}{{println .}}{{end}}' "
        "2>/dev/null | grep -E '^ee[0-9a-f]+$' | head -1 || true",
        "existing mtg secret", timeout=30).strip()
    if not secret:
        secret = run(c, f"docker run --rm {MTPROXY_IMAGE} generate-secret {fake_tls_domain}",
                     "generate mtg secret", timeout=120).strip().splitlines()[-1].strip()
    if not secret.startswith("ee"):
        raise SystemExit(f"[{host}] mtg generate-secret returned no ee-secret: {secret!r}")
    print(f"  [{host}] mtproxy: running mtg on :{MTPROXY_PORT}…")
    run(c, "docker rm -f aegis-mtg 2>/dev/null || true; "
           f"docker run -d --name aegis-mtg --network host --restart unless-stopped "
           f"{MTPROXY_IMAGE} simple-run 0.0.0.0:{MTPROXY_PORT} {secret} 2>&1 | tail -1",
        "run mtg", timeout=120)
    sync_bot_db_mtproxy(main_c, server_id, secret, MTPROXY_PORT)
    print(f"  [{host}] mtproxy up + DB synced (server {server_id}); "
          f"open UDP/TCP {MTPROXY_PORT} in the node firewall if one is active")
    return secret


def sync_bot_db_mtproxy(
    main_c: paramiko.SSHClient, server_id: int, secret: str, port: int
) -> None:
    """Mirror an mtg secret + port into the bot DB (servers row `server_id`)."""
    py = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.core.database import async_session_maker\n"
        f"SID={int(server_id)}\nSECRET={secret!r}\nPORT={int(port)}\n"
        "async def q():\n"
        "    async with async_session_maker() as s:\n"
        "        r=await s.execute(text('UPDATE servers SET mtproxy_secret=:sec, "
        "mtproxy_port=:p WHERE id=:i'), {'sec':SECRET,'p':PORT,'i':SID})\n"
        "        await s.commit(); print('rows updated:', r.rowcount)\n"
        "asyncio.run(q())\n"
    )
    out = run(main_c, "docker exec -i aegis-bot python3 <<'PYEOF'\n" + py + "PYEOF\n",
              f"db mtproxy sync {server_id}", timeout=60)
    print("   ", out.strip())
    if "rows updated: 0" in out:
        raise SystemExit(f"[mtproxy] no servers row id={server_id} — DB NOT updated")


def sync_bot_db_hy2(
    main_c: paramiko.SSHClient, host: str, obfs_password: str, hy2_sni: str
) -> None:
    """D) Mirror this node's Hy2 + tcp-inbound config into its bot DB row.

    Matches the servers row by host=<node IP> and writes the multi-inbound /
    Hy2 capability columns. The obfs_password MUST be the exact value rendered
    into the node's data/hysteria/config.yaml (returned by provision_hysteria),
    so the client config the bot emits matches what the server expects.
    """
    py = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.core.database import async_session_maker\n"
        f"HOST = {host!r}\n"
        f"OBFS = {obfs_password!r}\n"
        f"SNI = {hy2_sni!r}\n"
        f"TCP_PORT = {XRAY_TCP_PORT}\n"
        f"HY2_PORT = {HY2_LISTEN_PORT}\n"
        f"HOP_START = {HY2_HOP_START}\n"
        f"HOP_END = {HY2_HOP_END}\n"
        f"UP = {HY2_BW_UP!r}\n"
        f"DOWN = {HY2_BW_DOWN!r}\n"
        "async def q():\n"
        "    async with async_session_maker() as s:\n"
        "        res = await s.execute(text('UPDATE servers SET '\n"
        "            'tcp_port=:tcp, hy2_enabled=1, hy2_port=:hp, '\n"
        "            'hy2_hop_start=:hs, hy2_hop_end=:he, hy2_up=:up, '\n"
        "            'hy2_down=:down, hy2_obfs_password=:obfs, hy2_sni=:sni WHERE host=:h'),\n"
        "            {'tcp': TCP_PORT, 'hp': HY2_PORT, 'hs': HOP_START, 'he': HOP_END,\n"
        "             'up': UP, 'down': DOWN, 'obfs': OBFS, 'sni': SNI, 'h': HOST})\n"
        "        await s.commit()\n"
        "        print('rows updated:', res.rowcount)\n"
        "        r = await s.execute(text('SELECT id, name, host, tcp_port, '\n"
        "            'hy2_enabled, hy2_port, hy2_hop_start, hy2_hop_end FROM '\n"
        "            'servers WHERE host=:h'), {'h': HOST})\n"
        "        for row in r.fetchall(): print('  now:', tuple(row))\n"
        "asyncio.run(q())\n"
    )
    cmd = "docker exec -i aegis-bot python3 <<'PYEOF'\n" + py + "PYEOF\n"
    out = run(main_c, cmd, f"db hy2 sync {host}", timeout=60)
    print(out.rstrip())
    if "rows updated: 0" in out:
        raise SystemExit(f"[{host}] no servers row matched host={host} — DB NOT updated")


def verify_stack(c: paramiko.SSHClient, host: str) -> None:
    """Best-effort end-of-run verification (fails loudly on a hard miss)."""
    print(f"  [{host}] verify: hysteria container…")
    out = run(c, "docker ps --filter name=aegis-hysteria "
                 "--format '{{.Names}} {{.Status}}'", "ps hysteria", timeout=30)
    if "aegis-hysteria" not in out:
        raise SystemExit(f"[{host}] hysteria container is NOT running:\n{out.strip()}")
    print(f"    {out.strip()}")

    print(f"  [{host}] verify: /hy2/auth reachable on the agent…")
    # The agent container restarted in step C; retry a few times so it has time
    # to come up. A non-000 code proves the endpoint is up. Do NOT use
    # `|| echo 000` — it doubles curl's own 000 into "000000" and slips the check.
    code = run(c,
        "for i in 1 2 3 4 5 6; do "
        "c=$(curl -s -o /dev/null -w '%{http_code}' -m 5 -X POST "
        "http://127.0.0.1:8444/hy2/auth -H 'Content-Type: application/json' "
        "-d '{\"auth\":\"x\",\"tx\":0}'); "
        "[ \"$c\" != \"000\" ] && { echo \"$c\"; break; }; sleep 2; done",
        "curl hy2/auth", timeout=45).strip()
    code = code.splitlines()[-1].strip() if code else ""
    if not code or code == "000":
        raise SystemExit(f"[{host}] /hy2/auth unreachable (http_code={code!r})")
    print(f"    /hy2/auth http_code={code}")

    print(f"  [{host}] verify: 3 vless inbounds present…")
    out = run(c,
        "docker exec aegis-xray sh -c "
        "'grep -c \"\\\"protocol\\\": \\\"vless\\\"\" "
        "\"${XRAY_CONFIG_PATH:-/etc/xray/config.json}\"' 2>/dev/null || echo 0",
        "count vless", timeout=30).strip()
    print(f"    vless inbound count: {out}")
    # Greece runs 2 vless inbounds today (primary + tcp); gRPC is dropped. Warn
    # rather than hard-fail so a 2-vs-3 expectation drift doesn't abort a good run.
    try:
        n = int(out.splitlines()[-1])
    except (ValueError, IndexError):
        n = 0
    if n < 2:
        raise SystemExit(f"[{host}] expected >=2 vless inbounds, found {n}")


def provision_firewall(c: paramiko.SSHClient, host: str) -> None:
    """Open the stack's inbound ports when a UFW firewall is active. Some
    providers (e.g. HOSTKEY) ship UFW with a default-DROP policy, which silently
    blocks the tcp+VISION inbound (XRAY_TCP_PORT/tcp) and the Hysteria2 listener
    (HY2_LISTEN_PORT/udp) even though xray/hysteria are bound — the symptom is
    "only xhttp works" / "Hy2 N/D". No-op when ufw is inactive/absent (the other
    providers leave INPUT ACCEPT)."""
    status = run(c, "ufw status 2>/dev/null | head -1 || true", "ufw status", timeout=30)
    if "Status: active" not in status:
        print(f"  [{host}] firewall: ufw inactive/absent — nothing to open")
        return
    print(f"  [{host}] firewall: ufw active — opening {XRAY_TCP_PORT}/tcp + "
          f"{HY2_LISTEN_PORT}/udp")
    run(c, f"ufw allow {XRAY_TCP_PORT}/tcp >/dev/null 2>&1 || true; "
           f"ufw allow {HY2_LISTEN_PORT}/udp >/dev/null 2>&1 || true; "
           f"ufw reload >/dev/null 2>&1 || true; "
           f"ufw status | grep -E '{XRAY_TCP_PORT}|{HY2_LISTEN_PORT}' | head -4",
        "ufw allow", timeout=60)


def provision_stack(
    c: paramiko.SSHClient, host: str, geo_sni: str,
    cert_b64: str, key_b64: str,
    with_mtproxy: bool = False,
) -> str:
    """Orchestrate A->C on the node. Returns the OBFS_PASSWORD for the DB sync.

    Order matters: agent.env first (geo-SNI + stats secret), then hysteria
    (renders config with that secret), then install the REAL shared LE cert
    (cert_b64/key_b64 — overwrites provision_hysteria's self-signed placeholder so
    real clients accept it), then code (recreates agent -> rebuilds the
    multi-inbound config incl. the :2053 tcp+VISION inbound, then reloads xray).
    The bot DB sync (D) runs separately on the main host.
    """
    # Upload the compose FIRST — provision_hysteria's `docker compose up -d
    # hysteria` (step B) needs the hysteria service def + the tobyxdd image
    # already in the node's compose, which provision_code (C) would otherwise
    # only upload afterwards (so a fresh node's first hysteria up used the stale
    # compose and silently created no container).
    print(f"[provision-stack {host}] === uploading docker-compose.yml ===")
    upload(c, COMPOSE_LOCAL, REMOTE_COMPOSE)
    print(f"[provision-stack {host}] === A) agent.env ===")
    stats_secret = provision_agent_env(c, host, geo_sni)
    print(f"[provision-stack {host}] === firewall (open ports if ufw active) ===")
    provision_firewall(c, host)
    print(f"[provision-stack {host}] === B) hysteria ===")
    obfs_password = provision_hysteria(c, host, geo_sni, stats_secret)
    print(f"[provision-stack {host}] === LE cert (install shared cert, replaces self-signed) ===")
    install_hy2_cert(c, host, cert_b64, key_b64)
    if with_mtproxy:
        print(f"[provision-stack {host}] === (mtproxy) reserved — run "
              f"`--mtproxy SERVER_ID:IP:PASSWORD` separately ===")
    print(f"[provision-stack {host}] === C) code + restart ===")
    provision_code(c, host)
    print(f"[provision-stack {host}] === verify ===")
    verify_stack(c, host)
    return obfs_password


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_mtproxy(c: paramiko.SSHClient, host: str, main_c: paramiko.SSHClient, server_id: int) -> str | None:
    """Generate an MTProxy secret, start mtg on port 80, store secret in DB. Returns secret or None."""
    print(f"  [{host}] generating MTProxy secret…")
    _, out, _ = c.exec_command("docker run --rm ghcr.io/9seconds/mtg:2 generate-secret google.com 2>/dev/null", timeout=60)
    rc = out.channel.recv_exit_status()
    secret = out.read().decode().strip()
    if rc != 0 or not secret:
        print(f"  [{host}] failed to generate secret, skipping MTProxy")
        return None

    print(f"  [{host}] starting mtg on port 80…")
    run(c,
        f"docker rm -f aegis-mtg 2>/dev/null || true && "
        f"docker run -d --name aegis-mtg --network host --restart unless-stopped "
        f"ghcr.io/9seconds/mtg:2 run {secret} --bind 0.0.0.0:80 2>&1 | tail -1",
        "start mtg", timeout=60)

    # Save secret to bot DB
    update_cmd = (
        f"docker exec aegis-bot python3 -c "
        f"'import asyncio; from sqlalchemy import text; from src.core.database import async_session_maker\n"
        f"exec(\"\"\"async def q():\\n"
        f"    async with async_session_maker() as s:\\n"
        f"        await s.execute(text(\\\"UPDATE servers SET mtproxy_secret=\\\\\\\"{secret}\\\\\\\" WHERE id={server_id}\\\"))\\n"
        f"        await s.commit()\\n"
        f"asyncio.run(q())\"\"\")'"
    )
    run(main_c, update_cmd, "save secret", timeout=30)
    print(f"  [{host}] MTProxy ready ✓  secret={secret[:8]}…")
    return secret


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Push code updates to AegisVPN nodes.")
    p.add_argument("--main-host", default="89.125.181.236")
    p.add_argument("--main-password", required=False)
    p.add_argument("--bot", action="store_true", help="Update the bot on the main VPS")
    p.add_argument("--nodes", action="store_true", help="Update agent on --node targets")
    p.add_argument(
        "--node", action="append", dest="nodes_list", metavar="IP:PASSWORD",
        help="Node to update (repeatable). Format: ip:password",
    )
    p.add_argument(
        "--set-network", choices=["xhttp", "tcp"], default=None,
        help="Switch the Xray transport on --node targets and mirror the matching "
             "reality keys into the bot DB. Requires --main-password.",
    )
    p.add_argument(
        "--xhttp-mode", choices=["auto", "stream-one", "stream-up", "packet-up"],
        default="auto",
        help="Server xhttp mode when --set-network xhttp (default: auto). auto is "
             "the only non-breaking value — it accepts existing packet-up clients "
             "plus new stream-up/stream-one ones. An explicit mode 400s mismatched "
             "clients, so only use it when re-issuing every subscription.",
    )
    p.add_argument(
        "--mtproxy", action="store_true",
        help=f"Provision a fake-TLS MTProto proxy (mtg) on the reserved port "
             f"{MTPROXY_PORT} on --node targets and mirror its secret+port into "
             "the bot DB. Requires --main-password and --node SERVER_ID:IP:PASSWORD.",
    )
    p.add_argument(
        "--renew-hy2-cert", action="store_true",
        help="Renew the shared Hy2 Let's Encrypt cert on the ACME host "
             "(--main-host) via acme.sh --cron, then install it on every --node "
             "(IP:PASSWORD) and restart hysteria. Run before the cert expires.",
    )
    p.add_argument(
        "--split-migrate", action="store_true",
        help="One-time: migrate --node targets from the single vpn container to "
             "the split xray+agent topology (zero-drop code deploys afterwards). "
             "Uploads docker-compose.yml + agent sources. One brief xray blip.",
    )
    p.add_argument(
        "--provision-stack", action="store_true",
        help="Roll the full Greece canary stack (xray tcp+VISION :2053 + "
             "Hysteria2) onto --node targets and sync the bot DB. Idempotent + "
             "re-runnable. Requires --main-password (for the DB write).",
    )
    p.add_argument(
        "--geo-sni", default=None, metavar="NAME",
        help="Override the Hysteria2 self-signed cert CN / geo-SNI for "
             "--provision-stack (e.g. csc.fi, www.chalmers.se, uio.no, "
             "aegean.gr, www.osaka-u.ac.jp). Falls back to the built-in IP map.",
    )
    p.add_argument(
        "--with-mtproxy", action="store_true",
        help="(reserved) Also run the future MTProto proxy hook during "
             "--provision-stack. Currently a no-op stub.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not any((args.bot, args.nodes, args.mtproxy, args.set_network,
                args.split_migrate, args.provision_stack, args.renew_hy2_cert)):
        raise SystemExit("Specify --bot, --nodes, --mtproxy, --set-network, "
                         "--split-migrate, --provision-stack, and/or --renew-hy2-cert")
    if args.renew_hy2_cert and (not args.main_password or not args.nodes_list):
        raise SystemExit("--renew-hy2-cert requires --main-password (ACME host) "
                         "and at least one --node IP:PASSWORD to install onto")
    if args.bot and not args.main_password:
        raise SystemExit("--bot requires --main-password")
    if args.provision_stack and not args.nodes_list:
        raise SystemExit("--provision-stack requires at least one --node IP:PASSWORD")
    if args.provision_stack and not args.main_password:
        raise SystemExit("--provision-stack requires --main-password (to update the bot DB)")
    if (args.nodes or args.mtproxy) and not args.nodes_list:
        raise SystemExit("--nodes/--mtproxy requires at least one --node")
    if args.mtproxy and not args.main_password:
        raise SystemExit("--mtproxy requires --main-password (to update the bot DB)")
    if args.split_migrate and not args.nodes_list:
        raise SystemExit("--split-migrate requires at least one --node IP:PASSWORD")
    if args.set_network and not args.nodes_list:
        raise SystemExit("--set-network requires at least one --node IP:PASSWORD")
    if args.set_network and not args.main_password:
        raise SystemExit("--set-network requires --main-password (to update the bot DB)")

    if args.bot:
        print(f"[bot] connecting to {args.main_host}…")
        c = connect(args.main_host, args.main_password)
        try:
            update_bot(c)
        finally:
            c.close()

    if args.split_migrate:
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            print(f"[split-migrate {ip}] connecting…")
            c = connect(ip, password)
            try:
                split_migrate(c, ip)
            finally:
                c.close()

    if args.set_network:
        results: list[tuple[str, str, str]] = []  # (ip, pubkey, sid)
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            print(f"[set-network {ip}] connecting…")
            c = connect(ip, password)
            try:
                pubkey, sid = set_network(c, ip, args.set_network, args.xhttp_mode)
                results.append((ip, pubkey, sid))
            finally:
                c.close()

        print(f"[db] connecting to {args.main_host}…")
        mc = connect(args.main_host, args.main_password)
        try:
            for ip, pubkey, sid in results:
                set_db_keys(mc, ip, pubkey, sid)
        finally:
            mc.close()

    if args.provision_stack:
        # Fetch the shared Hy2 LE cert ONCE from the ACME host (= main_host) before
        # the per-node loop; provision_stack installs it on each node so a fresh
        # node serves the REAL cert (not provision_hysteria's self-signed one). The
        # cert's domain becomes the bot DB hy2_sni so hy2_capable turns True.
        print(f"[provision-stack] fetching shared Hy2 LE cert from ACME host {args.main_host}…")
        acme_c = connect(args.main_host, args.main_password)
        try:
            cert_b64, key_b64, hy2_domain = renew_hy2_cert_acme(acme_c)
        finally:
            acme_c.close()
        # (ip, obfs_password) pairs to mirror into the bot DB after the per-node
        # provisioning succeeds. The DB write is deferred to one main-host
        # session so a partial node failure never half-writes the DB.
        stack_results: list[tuple[str, str]] = []
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            geo_sni = resolve_geo_sni(ip, args.geo_sni)
            print(f"[provision-stack {ip}] connecting… (geo-SNI {geo_sni})")
            c = connect(ip, password)
            try:
                obfs = provision_stack(c, ip, geo_sni, cert_b64, key_b64,
                                       with_mtproxy=args.with_mtproxy)
                stack_results.append((ip, obfs))
            finally:
                c.close()

        print(f"[db] connecting to {args.main_host}… (D: bot DB sync)")
        mc = connect(args.main_host, args.main_password)
        try:
            for ip, obfs in stack_results:
                sync_bot_db_hy2(mc, ip, obfs, hy2_domain)
        finally:
            mc.close()

    if args.renew_hy2_cert:
        print(f"[renew-hy2] connecting to ACME host {args.main_host}…")
        mc = connect(args.main_host, args.main_password)
        try:
            cert_b64, key_b64, _ = renew_hy2_cert_acme(mc)
        finally:
            mc.close()
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if len(parts) < 2:
                raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
            ip, password = parts[0], ":".join(parts[1:])
            print(f"[renew-hy2 {ip}] installing cert…")
            c = connect(ip, password)
            try:
                install_hy2_cert(c, ip, cert_b64, key_b64)
            finally:
                c.close()

    main_c: paramiko.SSHClient | None = None

    if args.nodes or args.mtproxy:
        for node_str in args.nodes_list:
            parts = node_str.split(":")
            if args.mtproxy:
                # Format: SERVER_ID:IP:PASSWORD
                if len(parts) < 3:
                    raise SystemExit(f"--mtproxy expects SERVER_ID:IP:PASSWORD, got: {node_str}")
                server_id, ip, password = int(parts[0]), parts[1], ":".join(parts[2:])
            else:
                # Format: IP:PASSWORD
                if len(parts) < 2:
                    raise SystemExit(f"Bad --node format (expected IP:PASSWORD): {node_str}")
                ip, password = parts[0], ":".join(parts[1:])
                server_id = 0

            print(f"[node {ip}] connecting…")
            c = connect(ip, password)
            try:
                if args.nodes:
                    update_agent(c, ip)
                if args.mtproxy:
                    if main_c is None:
                        main_c = connect(args.main_host, args.main_password)
                    provision_mtproxy(c, ip, main_c, server_id)
            finally:
                c.close()

    if main_c is not None:
        main_c.close()

    print("\nAll done.")


if __name__ == "__main__":
    main()
