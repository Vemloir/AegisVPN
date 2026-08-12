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

  # Roll the full canary stack (xray raw-TCP+VISION :2053 + Hysteria2)
  # onto a node and sync its bot DB row. Idempotent + safe to re-run. The
  # geo-SNI (Hy2 cert CN) is REQUIRED via --geo-sni (never hardcoded — public repo):
  python update.py --provision-stack \\
                   --main-host MAIN_IP --main-password '…' \\
                   --node NODE_IP:ROOT_PASSWORD --geo-sni csc.fi
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import posixpath
import re
import secrets
import shlex
import stat
import time
from datetime import datetime
from ipaddress import IPv4Address, ip_address
from pathlib import Path

try:
    import paramiko
except ImportError:  # Allows offline unit tests of pure rollout logic.
    paramiko = None  # type: ignore[assignment]

try:
    from .control_plane import (
        PromotionState,
        render_agent_firewall,
        validate_promotion,
    )
except ImportError:  # Direct execution: python deploy/vps/update.py
    from control_plane import (
        PromotionState,
        render_agent_firewall,
        validate_promotion,
    )

ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = ROOT / "agent"
BOT_DIR = ROOT / "bot"
SYSTEMD_DIR = ROOT / "deploy/vps/systemd"
HY2_EXPORT_SCRIPT = ROOT / "deploy/vps/export_hy2_certificate.py"
RUNTIME_VERSIONS_FILE = ROOT / "deploy/vps/runtime-versions.env"

_REQUIRED_RUNTIME_KEYS = {
    "PYTHON_IMAGE",
    "UV_IMAGE",
    "XRAY_VERSION",
    "XRAY_SHA256",
    "XRAY_ARM64_SHA256",
    "HYSTERIA_VERSION",
    "HYSTERIA_IMAGE",
    "CADDY_IMAGE",
    "MTG_IMAGE",
}


def verify_runtime_pins(
    runtime_file: Path = RUNTIME_VERSIONS_FILE,
    xray_archive: Path | None = None,
) -> dict[str, str]:
    """Validate immutable runtime references and optionally an Xray archive."""
    values: dict[str, str] = {}
    for raw_line in runtime_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or not value:
            raise ValueError(f"invalid runtime manifest line: {raw_line!r}")
        values[key] = value

    missing = sorted(_REQUIRED_RUNTIME_KEYS - values.keys())
    if missing:
        raise ValueError(f"runtime manifest missing: {', '.join(missing)}")
    for key in ("PYTHON_IMAGE", "UV_IMAGE", "HYSTERIA_IMAGE", "CADDY_IMAGE", "MTG_IMAGE"):
        if not re.search(r"@sha256:[0-9a-f]{64}$", values[key]):
            raise ValueError(f"{key} is not pinned by sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{64}", values["XRAY_SHA256"]):
        raise ValueError("XRAY_SHA256 is not a sha256 digest")
    if not re.fullmatch(r"[0-9a-f]{64}", values["XRAY_ARM64_SHA256"]):
        raise ValueError("XRAY_ARM64_SHA256 is not a sha256 digest")

    if xray_archive is not None:
        actual = hashlib.sha256(xray_archive.read_bytes()).hexdigest()
        if actual != values["XRAY_SHA256"]:
            raise ValueError(
                f"Xray checksum mismatch: expected {values['XRAY_SHA256']}, got {actual}"
            )
    return values


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def connect(
    host: str,
    password: str,
    attempts: int = 4,
    *,
    username: str = "root",
) -> paramiko.SSHClient:
    if paramiko is None:
        raise SystemExit("paramiko required: pip install paramiko")
    delay = 8.0
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        c = paramiko.SSHClient()
        c.load_system_host_keys()
        c.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            c.connect(
                host,
                username=username,
                password=password,
                timeout=30,
                banner_timeout=30,
                auth_timeout=30,
            )
            return c
        except Exception as exc:
            last = exc
            try:
                c.close()
            except Exception:
                pass
            if attempt == attempts:
                break
            print(f"  ssh retry {attempt}/{attempts-1} ({exc}) sleep {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 1.7, 45.0)
    raise SystemExit(f"cannot reach {host}: {last}")


def connect_via_jump(
    jump_client: paramiko.SSHClient,
    host: str,
    password: str,
    *,
    username: str = "root",
) -> paramiko.SSHClient:
    """Connect through a verified SSH jump host while still pinning the target."""
    if paramiko is None:
        raise SystemExit("paramiko required: pip install paramiko")
    transport = jump_client.get_transport()
    if transport is None or not transport.is_active():
        raise SystemExit("SSH jump transport is not active")
    channel = transport.open_channel(
        "direct-tcpip",
        (host, 22),
        ("127.0.0.1", 0),
        timeout=30,
    )
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            host,
            username=username,
            password=password,
            sock=channel,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )
    except BaseException:
        client.close()
        channel.close()
        raise
    return client


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
    """Upload bot/control API sources and recreate both control-host processes.

    ``siteapi`` serves ``/api/node/v1/*`` while ``bot`` runs migrations and the
    Telegram workflows. They share one image source tree, so deploying only one
    leaves half of the control plane on stale code. Neither operation touches
    Xray.
    """
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
    for manifest in ("Dockerfile.deploy", "pyproject.toml", "uv.lock"):
        src = BOT_DIR / manifest
        if src.exists():
            upload(c, src, f"/root/aegis/bot/{manifest}")

    # Recreate bot container WITHOUT going through docker compose up
    # (which would also restart aegis-vpn via depends_on).
    print("  rebuilding image…")
    run(
        c,
        "cd /root/aegis/deploy/vps && "
        "docker compose build bot siteapi 2>&1 | tail -5",
        "docker build bot/siteapi",
        timeout=300,
    )

    print("  recreating bot + siteapi containers (vpn untouched)…")
    run(
        c,
        "docker stop aegis-bot aegis-siteapi 2>/dev/null || true; "
        "docker rm aegis-bot aegis-siteapi 2>/dev/null || true",
        "remove old bot/siteapi",
    )
    run(
        c,
        "cd /root/aegis/deploy/vps && "
        "docker compose up -d --no-deps bot siteapi 2>&1",
        "start bot/siteapi",
        timeout=90,
    )
    _install_control_hy2_certificate_export(c)
    print("  bot + siteapi updated ✓")


# ---------------------------------------------------------------------------
# Agent (node) update
# ---------------------------------------------------------------------------

COMPOSE_LOCAL = ROOT / "deploy/vps/docker-compose.yml"
REMOTE_COMPOSE = "/root/aegis/deploy/vps/docker-compose.yml"
# The running xray config. Rendered from template.json ONCE, at first boot, and
# never again — it carries the node's live client list.
REMOTE_XRAY_CONFIG = "/root/aegis/deploy/vps/data/vpn/xray-config.json"
# agent.env is generated on the persistent data volume; vpn.env is Compose's
# host-side environment. Keep both aligned so no later Agent recreate can
# silently restore an obsolete runtime value.
REMOTE_AGENT_ENV = "/root/aegis/deploy/vps/data/vpn/agent.env"
REMOTE_VPN_ENV = "/root/aegis/deploy/vps/vpn.env"


def apply_stability_profile(live: dict, template: dict) -> dict:
    """Apply latency/stability settings without touching node identity or users."""
    patched = copy.deepcopy(live)
    desired_level = template["policy"]["levels"]["0"]
    level = (
        patched.setdefault("policy", {})
        .setdefault("levels", {})
        .setdefault("0", {})
    )
    level["handshake"] = desired_level["handshake"]
    level["connIdle"] = desired_level["connIdle"]
    patched["dns"] = copy.deepcopy(template["dns"])

    routing = patched.setdefault("routing", {})
    routing["domainStrategy"] = template["routing"]["domainStrategy"]

    def is_removed_rule(rule: dict) -> bool:
        blocks_quic = (
            rule.get("outboundTag") == "block"
            and rule.get("network") == "udp"
            and str(rule.get("port")) == "443"
        )
        redundant_ru_direct = rule.get("outboundTag") == "direct" and (
            "geoip:ru" in rule.get("ip", [])
            or "geosite:category-ru" in rule.get("domain", [])
        )
        return blocks_quic or redundant_ru_direct

    routing["rules"] = [
        rule for rule in routing.get("rules", []) if not is_removed_rule(rule)
    ]
    return patched


def update_env_values(blob: str, updates: dict[str, str]) -> str:
    """Replace selected env values while preserving comments and ordering."""
    remaining = dict(updates)
    output: list[str] = []
    for line in blob.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output) + "\n"


def _read_remote_text(c: paramiko.SSHClient, path: str) -> str:
    with get_sftp(c).open(path, "r") as handle:
        data = handle.read()
    return data.decode() if isinstance(data, bytes) else data


def _write_remote_text_atomic(
    c: paramiko.SSHClient,
    path: str,
    text: str,
    mode: int = 0o600,
) -> None:
    sftp = get_sftp(c)
    temporary = f"{path}.tmp-{time.time_ns()}"
    with sftp.open(temporary, "w") as handle:
        handle.write(text)
    sftp.chmod(temporary, mode)
    sftp.posix_rename(temporary, path)


def patch_node_stability(c: paramiko.SSHClient, host: str) -> None:
    """Atomically apply the stable policy/routing/DNS profile to a live node."""
    template = json.loads((AGENT_DIR / "template.json").read_text())
    live = json.loads(_read_remote_text(c, REMOTE_XRAY_CONFIG))
    patched = apply_stability_profile(live, template)

    # These are the data-plane identity and authorization boundaries. Abort
    # locally before uploading if the patch would alter either one.
    if patched.get("inbounds") != live.get("inbounds"):
        raise SystemExit(f"[{host}] stability patch changed inbounds; aborting")
    if patched.get("outbounds") != live.get("outbounds"):
        raise SystemExit(f"[{host}] stability patch changed outbounds; aborting")

    candidate_path = REMOTE_XRAY_CONFIG + ".candidate.json"
    backup_path = (
        REMOTE_XRAY_CONFIG
        + ".backup-"
        + datetime.now().strftime("%Y%m%dT%H%M%SZ")
    )
    agent_env = _read_remote_text(c, REMOTE_AGENT_ENV)
    vpn_env = _read_remote_text(c, REMOTE_VPN_ENV)

    _write_remote_text_atomic(
        c,
        candidate_path,
        json.dumps(patched, indent=2) + "\n",
    )
    run(
        c,
        f"cp --preserve=mode,ownership,timestamps "
        f"{shlex.quote(REMOTE_XRAY_CONFIG)} {shlex.quote(backup_path)}",
        "backup live xray config",
    )
    run(
        c,
        "docker exec aegis-xray xray run -test "
        "-c /data/xray-config.json.candidate.json",
        "validate stability candidate",
        timeout=60,
    )

    activated = False
    try:
        run(
            c,
            f"mv {shlex.quote(candidate_path)} {shlex.quote(REMOTE_XRAY_CONFIG)}",
            "activate stability candidate",
        )
        activated = True
        _write_remote_text_atomic(
            c,
            REMOTE_AGENT_ENV,
            update_env_values(agent_env, {"XRAY_CONN_IDLE": "300"}),
        )
        _write_remote_text_atomic(
            c,
            REMOTE_VPN_ENV,
            update_env_values(vpn_env, {"XRAY_CONN_IDLE": "300"}),
        )
        run(
            c,
            "cd /root/aegis/deploy/vps && "
            "docker compose restart xray 2>&1 | tail -3",
            "restart xray",
            timeout=120,
        )
        health = run(
            c,
            "for attempt in $(seq 1 20); do "
            "curl -fsS --max-time 2 http://127.0.0.1:8444/health && exit 0; "
            "sleep 1; "
            "done; exit 1",
            "post-stability health",
            timeout=60,
        )
    except BaseException:
        if activated:
            _write_remote_text_atomic(c, REMOTE_AGENT_ENV, agent_env)
            _write_remote_text_atomic(c, REMOTE_VPN_ENV, vpn_env)
            run(
                c,
                f"cp --preserve=mode,ownership,timestamps "
                f"{shlex.quote(backup_path)} {shlex.quote(REMOTE_XRAY_CONFIG)}",
                "restore xray config",
            )
            run(
                c,
                "cd /root/aegis/deploy/vps && "
                "docker compose restart xray 2>&1 | tail -3",
                "restart restored xray",
                timeout=120,
            )
        raise

    client_count = sum(
        len(inbound.get("settings", {}).get("clients", []))
        for inbound in patched.get("inbounds", [])
    )
    print(
        f"  [{host}] stability profile active; "
        f"{client_count} client records preserved; health={health.strip()}"
    )


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


def _install_control_hy2_certificate_export(c: paramiko.SSHClient) -> None:
    upload(
        c,
        HY2_EXPORT_SCRIPT,
        "/root/aegis/deploy/vps/export_hy2_certificate.py",
    )
    for name in (
        "aegis-hy2-cert-export.service",
        "aegis-hy2-cert-export.timer",
    ):
        upload(c, SYSTEMD_DIR / name, f"/etc/systemd/system/{name}")
    run(
        c,
        "systemctl daemon-reload && "
        "systemctl enable --now aegis-hy2-cert-export.timer",
        "enable Hy2 certificate export",
    )


def _install_node_hy2_certificate_reload(c: paramiko.SSHClient) -> None:
    for name in (
        "aegis-hy2-cert-reload.service",
        "aegis-hy2-cert-reload.path",
    ):
        upload(c, SYSTEMD_DIR / name, f"/etc/systemd/system/{name}")
    run(
        c,
        "systemctl daemon-reload && "
        "systemctl enable --now aegis-hy2-cert-reload.path",
        "enable Hy2 certificate reload",
    )


def patch_node_dns(c: paramiko.SSHClient, host: str) -> None:
    """Replace the DNS block of a LIVE node's xray config, in place.

    Uploading a new template.json does nothing to a node already in service:
    entrypoint.sh only renders the template when no config exists yet ("Keep
    existing config to preserve current clients across restarts"). So a node keeps
    whatever resolver it was born with, and a template change is a silent no-op.

    This rewrites just the `dns` object of the running config — every client, key
    and inbound is left exactly as it is — and restarts xray to pick it up.
    Restarting xray drops the sessions on this node for a moment; that is the
    whole cost, and it is why this is a separate, explicit operation.
    """
    dns_block = json.loads((AGENT_DIR / "template.json").read_text())["dns"]

    print(f"  [{host}] patching the live xray config's DNS…")
    remote_py = (
        "import json,os,tempfile\n"
        f"p='{REMOTE_XRAY_CONFIG}'\n"
        "cfg=json.load(open(p))\n"
        f"cfg['dns']={json.dumps(dns_block)}\n"
        "d=os.path.dirname(p)\n"
        "fd,tmp=tempfile.mkstemp(dir=d)\n"
        "json.dump(cfg,os.fdopen(fd,'w'),indent=2)\n"
        "os.replace(tmp,p)\n"
        "print('clients kept:',sum(len(i.get('settings',{}).get('clients',[])) "
        "for i in cfg.get('inbounds',[])))\n"
        "print('dns now:',json.dumps(cfg['dns']))\n"
    )
    out = run(
        c,
        f"python3 -c {shlex.quote(remote_py)}",
        "patch dns",
        timeout=60,
    )
    print("    " + out.strip().replace("\n", "\n    "))

    print(f"  [{host}] restarting xray (brief session drop on this node)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose restart xray 2>&1 | tail -2",
        "restart xray", timeout=120)
    print(f"  [{host}] DNS patched ✓")


def update_agent(c: paramiko.SSHClient, host: str) -> None:
    """Upload agent sources and recreate ONLY the agent container.

    In the split topology xray runs in its own container, so rebuilding `agent`
    leaves the data plane (and every live client session) untouched — code
    deploys are zero-drop. The current Compose file is uploaded as well so
    existing split nodes receive new Agent-only mounts such as /data/control;
    `up --no-deps agent` still does not touch Xray or unrelated services. Run
    --split-migrate once first to create the split.
    """
    topology = run(
        c,
        "docker inspect aegis-xray >/dev/null 2>&1 && printf split || printf missing",
        "check split topology",
        timeout=30,
    ).strip()
    if topology != "split":
        raise SystemExit(
            f"[{host}] refusing agent-only update: split Xray container is missing; "
            "run --split-migrate first"
        )

    _upload_agent_sources(c, host)
    print(f"  [{host}] uploading current docker-compose.yml…")
    upload(c, COMPOSE_LOCAL, REMOTE_COMPOSE)
    print(f"  [{host}] rebuilding + recreating agent (xray untouched)…")
    run(c, "cd /root/aegis/deploy/vps && docker compose up -d --build --no-deps agent 2>&1 | tail -4",
        "agent rebuild", timeout=300)
    _install_node_hy2_certificate_reload(c)
    print(f"  [{host}] agent updated ✓")


def _set_node_control_settings(
    c: paramiko.SSHClient,
    *,
    mode: str,
    bind_host: str,
) -> None:
    values = {
        "CONTROL_MODE": mode,
        "AGENT_BIND_HOST": bind_host,
    }
    script = (
        "from pathlib import Path\n"
        "p=Path('/root/aegis/deploy/vps/vpn.env')\n"
        f"values={values!r}\n"
        "lines=p.read_text().splitlines()\n"
        "seen=set()\n"
        "out=[]\n"
        "for line in lines:\n"
        "    key=line.split('=',1)[0] if '=' in line else ''\n"
        "    if key in values:\n"
        "        out.append(f'{key}={values[key]}')\n"
        "        seen.add(key)\n"
        "    else:\n"
        "        out.append(line)\n"
        "for key,value in values.items():\n"
        "    if key not in seen:\n"
        "        out.append(f'{key}={value}')\n"
        "tmp=p.with_suffix('.env.tmp')\n"
        "tmp.write_text('\\n'.join(out)+'\\n')\n"
        "tmp.chmod(0o600)\n"
        "tmp.replace(p)\n"
    )
    run(
        c,
        f"python3 -c {shlex.quote(script)}",
        "update node control settings",
    )
    run(
        c,
        "cd /root/aegis/deploy/vps && "
        "docker compose up -d --no-deps --force-recreate agent 2>&1 | tail -3",
        "recreate agent only",
        timeout=120,
    )


def _apply_agent_firewall(
    c: paramiko.SSHClient,
    *,
    control_server_ip: str,
    allow_control_server: bool,
) -> None:
    script = "#!/bin/sh\nset -eu\n" + render_agent_firewall(
        control_server_ip=control_server_ip,
        public_agent=allow_control_server,
    )
    run(
        c,
        f"sh -c {shlex.quote(script)}",
        "apply Agent API firewall",
    )


def _fetch_promotion_state(
    main_c: paramiko.SSHClient,
    *,
    server_id: int,
) -> PromotionState:
    script = (
        "import json,sqlite3\n"
        "db='/root/aegis/deploy/vps/data/bot/aegis.db'\n"
        "con=sqlite3.connect(db)\n"
        "con.row_factory=sqlite3.Row\n"
        f"sid={server_id}\n"
        "row=con.execute('SELECT desired_generation,applied_generation,"
        "applied_digest,control_last_seen_at,control_last_error FROM servers "
        "WHERE id=?',(sid,)).fetchone()\n"
        "if row is None: raise SystemExit('server not found')\n"
        "snap=con.execute('SELECT digest FROM node_snapshots WHERE server_id=? "
        "AND generation=?',(sid,row['desired_generation'])).fetchone()\n"
        "print(json.dumps({'desired_generation':row['desired_generation'],"
        "'applied_generation':row['applied_generation'],"
        "'desired_digest':snap['digest'] if snap else None,"
        "'applied_digest':row['applied_digest'],"
        "'last_seen_at':row['control_last_seen_at'],"
        "'last_error':row['control_last_error']}))\n"
    )
    payload = json.loads(
        run(
            main_c,
            f"python3 -c {shlex.quote(script)}",
            "read promotion state",
        ).strip()
    )
    last_seen_raw = payload["last_seen_at"]
    return PromotionState(
        desired_generation=int(payload["desired_generation"]),
        applied_generation=int(payload["applied_generation"]),
        desired_digest=payload["desired_digest"],
        applied_digest=payload["applied_digest"],
        last_seen_at=(
            datetime.fromisoformat(last_seen_raw) if last_seen_raw else None
        ),
        last_error=payload["last_error"],
    )


def _set_server_control_mode(
    main_c: paramiko.SSHClient,
    *,
    server_id: int,
    mode: str,
) -> None:
    if mode not in {"observe", "pull"}:
        raise ValueError("central control mode must be observe or pull")
    script = (
        "import sqlite3\n"
        "db='/root/aegis/deploy/vps/data/bot/aegis.db'\n"
        "con=sqlite3.connect(db)\n"
        f"cur=con.execute('UPDATE servers SET control_mode=? WHERE id=?',"
        f"({mode!r},{server_id}))\n"
        "assert cur.rowcount == 1, 'server not found'\n"
        "con.commit()\n"
    )
    run(
        main_c,
        f"python3 -c {shlex.quote(script)}",
        f"set central node mode {mode}",
    )


def promote_pull(
    main_c: paramiko.SSHClient,
    node_c: paramiko.SSHClient,
    *,
    server_id: int,
    control_server_ip: str,
    timeout_seconds: int,
) -> None:
    """Apply via outbound control, wait for an exact ack, then close TCP/8444."""
    print("  switching node reconciler to apply (Agent API remains allowlisted)…")
    _set_node_control_settings(node_c, mode="apply", bind_host="0.0.0.0")
    _apply_agent_firewall(
        node_c,
        control_server_ip=control_server_ip,
        allow_control_server=True,
    )

    deadline = time.monotonic() + timeout_seconds
    last_error = "no acknowledgement received"
    while time.monotonic() < deadline:
        state = _fetch_promotion_state(main_c, server_id=server_id)
        try:
            validate_promotion(state, max_age_seconds=90)
        except ValueError as exc:
            last_error = str(exc)
            time.sleep(2)
            continue
        break
    else:
        raise SystemExit(f"promotion guard failed: {last_error}")

    print("  acknowledgement matches desired generation/digest; closing Agent API…")
    _set_node_control_settings(node_c, mode="apply", bind_host="127.0.0.1")
    _apply_agent_firewall(
        node_c,
        control_server_ip=control_server_ip,
        allow_control_server=False,
    )
    _set_server_control_mode(main_c, server_id=server_id, mode="pull")
    print("  pull promotion complete; Xray was not restarted ✓")


def rollback_observe(
    main_c: paramiko.SSHClient,
    node_c: paramiko.SSHClient,
    *,
    server_id: int,
    control_server_ip: str,
) -> None:
    """Restore observe/push without exposing TCP/8444 to the public Internet."""
    _set_server_control_mode(main_c, server_id=server_id, mode="observe")
    _set_node_control_settings(node_c, mode="observe", bind_host="0.0.0.0")
    _apply_agent_firewall(
        node_c,
        control_server_ip=control_server_ip,
        allow_control_server=True,
    )
    print("  rollback complete; Agent API is reachable only from control IP ✓")


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
        env.setdefault("XHTTP_PATH", "/")
        env["XHTTP_MODE"] = xhttp_mode
        env["XRAY_CONN_IDLE"] = "300"
    # ONE keypair per node, whatever the transport: entrypoint.sh builds every
    # inbound from PRIVATE_KEY/SHORT_ID. Selecting *_TCP here (as this used to for
    # tcp) published a public key whose private half the node never signs with —
    # a silent, total REALITY handshake failure for every client of that node.
    pubkey, sid = env.get("PUBLIC_KEY"), env.get("SHORT_ID")
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
# Encodes the steps that were done by hand on the first canary node so the same
# multi-inbound (xray tcp+VISION) + Hysteria2 stack rolls onto the other nodes
# with one command. Every step is IDEMPOTENT and COMPOSABLE:
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

# Per-node geo-SNI = the Hysteria2 self-signed cert CN (cosmetic — Hy2 clients
# set insecure=1, but a plausible CN keeps a passive probe boring). It is NOT
# hardcoded here: this is a PUBLIC repo, so node IPs and their SNIs must never be
# committed. Supply it per run with --geo-sni <name> — a geo-matched academic
# TLS1.3 + ALPN-h2 domain for the node's location. REALITY_SERVER_NAME/DEST are
# set separately via add_server.py's --reality-server-name / --reality-dest.

# --- Port plan -------------------------------------------------------------
# Reserve a stable, documented port layout so services never collide and a
# future Telegram (MTProto) proxy slots in cleanly:
#
#   443         xray primary inbound      (XRAY_PORT, current network)
#   2053        xray raw-TCP + VISION     (XRAY_TCP_PORT)
#   8443        Caddy HTTPS (subscription)
#   8444        agent API (loopback)      hy2 auth callback :8444/hy2/auth
#   9999        Hy2 trafficStats          (loopback)
#   443         Hysteria2 UDP listen (looks like HTTP/3 QUIC -> RU mobile passes)
#   --- reserved for the future MTProto proxy (mtg, `mtproxy` compose profile) ---
#   8765        mtg MTProto listen        (RESERVED — see provision_mtproxy)
#
XRAY_TCP_PORT = 2053
XRAY_CONN_IDLE = 300
# Hy2 now listens on UDP 443 with NO obfs, NO port hopping, BBR congestion (see
# hysteria/config.template.yaml). The old high-port + salamander + Brutal setup
# was dropped on RU mobile and bufferbloated CS latency.
HY2_LISTEN_PORT = 443
HY2_STATS_URL = "http://127.0.0.1:9999"
MTPROXY_PORT = 2083         # Cloudflare-HTTPS-alt port: mobile networks carry
                            # these reliably (like 2053), unlike high random
                            # ports (8765 was unreachable on mobile)

REMOTE_HY2_DIR = "/root/aegis/deploy/vps/data/hysteria"
REMOTE_HY2_CONFIG = f"{REMOTE_HY2_DIR}/config.yaml"
HY2_TEMPLATE_LOCAL = ROOT / "deploy/vps/hysteria/config.template.yaml"

# Hy2 TLS cert renewal (Let's Encrypt via acme.sh on the ACME host). The domain
# is never hardcoded here (public repo): the cert dir is discovered by globbing
# the single *_ecc directory acme.sh created.
REMOTE_ACME_DIR = "/root/acme"
ACME_IMAGE = "neilpang/acme.sh"
CADDY_CERT_ROOT = (
    "/root/aegis/deploy/vps/data/caddy/caddy/certificates"
)

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
    """The cert CN / geo-SNI for a node — supplied ONLY via --geo-sni. Never
    hardcoded by IP (this is a public repo), so the flag is required."""
    if override:
        return override
    raise SystemExit(
        f"[{host}] pass --geo-sni <name> — a geo-matched academic TLS1.3 + h2 "
        f"domain for this node's location (node IPs/SNIs are not hardcoded in "
        f"this public repo)."
    )


def provision_hy2_agent_env(c: paramiko.SSHClient, host: str) -> str:
    """Enable only Hy2 auth/stats settings without changing Xray transports."""
    if not _remote_exists(c, REMOTE_AGENT_ENV):
        raise SystemExit(
            f"[{host}] {REMOTE_AGENT_ENV} missing — provision the base node first"
        )
    current = _read_remote_text(c, REMOTE_AGENT_ENV)
    env = _parse_env(current)
    existing_secret = env.get("HY2_STATS_SECRET")
    stats_secret = existing_secret or _gen_secret(32)
    env["XRAY_CONN_IDLE"] = str(XRAY_CONN_IDLE)
    env["HY2_ENABLED"] = "true"
    env["HY2_STATS_URL"] = HY2_STATS_URL
    env["HY2_STATS_SECRET"] = stats_secret
    _write_remote_text_atomic(c, REMOTE_AGENT_ENV, _render_env(env))
    print(
        f"  [{host}] Hy2 Agent settings enabled; "
        f"stats secret {'reused' if existing_secret else 'created'}"
    )
    return stats_secret


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
    """B) Provision Hysteria2 on the node. Returns "" (obfs no longer used).

    Idempotent end to end:
      * data/hysteria/current/{key,cert}.pem self-signed PLACEHOLDER generated only if
        absent (CN = geo_sni); provision_stack overwrites it with the real LE
        cert via install_hy2_cert.
      * config.yaml rendered from the repo template (only the agent.env stats
        secret is injected) — UDP :443, no obfs, BBR (no bandwidth set).
      * `docker compose --profile hysteria up -d hysteria`.

    No port-hop REDIRECT any more (single UDP :443). Nodes provisioned under the
    old scheme keep a stale 20000-50000 -> 36500 nat rule + aegis-hy2-hop.service;
    those are harmless (nothing listens on 36500) and can be cleaned out of band.
    """
    print(f"  [{host}] hysteria: ensuring {REMOTE_HY2_DIR}")
    run(c, f"mkdir -p {REMOTE_HY2_DIR}", "mkdir hysteria")

    # --- UDP socket buffers (quic-go / hysteria need multi-MB) ---
    # The Linux default net.core.rmem_max/wmem_max is ~208 KB, far too small for
    # QUIC: under bursts the kernel drops UDP at the socket buffer (manifests as
    # client-side receive loss on the server->client path). hysteria's own docs
    # require raising these to ~16 MB. Persist + apply, idempotently.
    print(f"  [{host}] hysteria: setting UDP socket buffers to 16 MB (sysctl)")
    run(c,
        "printf 'net.core.rmem_max=16777216\\nnet.core.wmem_max=16777216\\n' "
        "> /etc/sysctl.d/99-hysteria.conf && "
        "sysctl -p /etc/sysctl.d/99-hysteria.conf >/dev/null 2>&1 || true",
        "sysctl udp buffers", timeout=30)

    # --- self-signed cert (idempotent) ---
    key_pem = f"{REMOTE_HY2_DIR}/current/key.pem"
    cert_pem = f"{REMOTE_HY2_DIR}/current/cert.pem"
    if _remote_exists(c, key_pem) and _remote_exists(c, cert_pem):
        print(f"  [{host}] hysteria: cert present, keeping it")
    else:
        print(f"  [{host}] hysteria: generating self-signed cert (CN={geo_sni})")
        version = f"bootstrap-{secrets.token_hex(8)}"
        run(
            c,
            f"cd {REMOTE_HY2_DIR} && mkdir -p versions/{version} && "
            f"chmod 700 versions/{version} && "
            f"openssl ecparam -genkey -name prime256v1 -out versions/{version}/key.pem && "
            f"openssl req -new -x509 -days 3650 -key versions/{version}/key.pem "
            f"-out versions/{version}/cert.pem -subj '/CN={geo_sni}' && "
            f"chmod 600 versions/{version}/cert.pem versions/{version}/key.pem && "
            f"ln -s versions/{version} .current-{version} && "
            f"mv -Tf .current-{version} current",
            "openssl cert",
            timeout=60,
        )

    # --- render config.yaml from the repo template (only the stats secret) ---
    template = HY2_TEMPLATE_LOCAL.read_text(encoding="utf-8")
    rendered = template.replace("__STATS_SECRET__", stats_secret)
    leftovers = [tok for tok in ("__STATS_SECRET__",) if tok in rendered]
    if leftovers:
        raise SystemExit(f"[{host}] hysteria config still has placeholders: {leftovers}")
    print(f"  [{host}] hysteria: rendering config.yaml (UDP :443, no obfs, BBR)")
    sftp = get_sftp(c)
    with sftp.open(REMOTE_HY2_CONFIG, "w") as fh:
        fh.write(rendered)

    # No port-hop REDIRECT: single UDP :443 (no obfs, no hopping).

    # --- start (or restart) the hysteria container ---
    print(f"  [{host}] hysteria: docker compose up -d hysteria")
    # No `| tail` here: a pipe masks docker-compose's exit code, so a failed
    # image pull would slip through unnoticed. Capture the real exit status.
    run(c,
        "cd /root/aegis/deploy/vps && "
        "docker compose --profile hysteria up -d hysteria 2>&1",
        "hysteria up", timeout=180)
    return ""  # obfs removed; the DB obfs_password column is unused now


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
    """Return a managed DuckDNS certificate for Hy2.

    Prefer Caddy's automatically renewed certificate. Fall back to the legacy
    acme.sh store when no Caddy-managed DuckDNS certificate exists.
    """
    caddy_cert = run(
        main_c,
        f"find {shlex.quote(CADDY_CERT_ROOT)} -type f "
        f"-path '*duckdns.org/*.crt' 2>/dev/null | head -1",
        "find caddy hy2 cert",
        timeout=30,
    ).strip()
    if caddy_cert:
        domain = posixpath.basename(caddy_cert)
        if domain.endswith(".crt"):
            domain = domain[:-4]
        caddy_key = caddy_cert[:-4] + ".key"
        match = run(
            main_c,
            f"cert_hash=$(openssl x509 -in {shlex.quote(caddy_cert)} "
            f"-noout -pubkey | sha256sum | cut -d' ' -f1); "
            f"key_hash=$(openssl pkey -in {shlex.quote(caddy_key)} "
            f"-pubout | sha256sum | cut -d' ' -f1); "
            f"test \"$cert_hash\" = \"$key_hash\" && echo MATCH",
            "verify caddy hy2 key",
            timeout=30,
        ).strip()
        if match != "MATCH":
            raise SystemExit("[caddy] Hy2 certificate/private-key mismatch")
        cert_b64 = run(
            main_c,
            f"base64 -w0 {shlex.quote(caddy_cert)}",
            "read caddy hy2 cert",
            timeout=30,
        ).strip()
        key_b64 = run(
            main_c,
            f"base64 -w0 {shlex.quote(caddy_key)}",
            "read caddy hy2 key",
            timeout=30,
        ).strip()
        enddate = run(
            main_c,
            f"openssl x509 -in {shlex.quote(caddy_cert)} -noout -enddate",
            "caddy hy2 cert enddate",
            timeout=30,
        ).strip()
        if not cert_b64 or not key_b64:
            raise SystemExit("[caddy] Hy2 certificate/private key is empty")
        print(f"  [caddy] current cert for {domain} {enddate}")
        return cert_b64, key_b64, domain

    print("  [acme] Caddy DuckDNS cert absent; trying acme.sh --cron…")
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
    """Atomically activate a verified LE cert/key pair and restart Hysteria."""
    cert_pem = base64.b64decode(cert_b64, validate=True).decode("ascii")
    key_pem = base64.b64decode(key_b64, validate=True).decode("ascii")
    version = secrets.token_hex(8)
    version_dir = f"{REMOTE_HY2_DIR}/versions/{version}"
    run(c, f"install -d -m 700 {version_dir}", "prepare cert version", timeout=30)
    _write_remote_text_atomic(
        c,
        f"{version_dir}/cert.pem",
        cert_pem,
        mode=0o600,
    )
    _write_remote_text_atomic(
        c,
        f"{version_dir}/key.pem",
        key_pem,
        mode=0o600,
    )
    match = run(c,
        f"openssl x509 -in {version_dir}/cert.pem -noout -pubkey > /tmp/_aegcp 2>/dev/null; "
        f"openssl pkey -in {version_dir}/key.pem -pubout > /tmp/_aegkp 2>/dev/null; "
        f"if diff -q /tmp/_aegcp /tmp/_aegkp >/dev/null 2>&1; then echo MATCH; else echo MISMATCH; fi; "
        f"rm -f /tmp/_aegcp /tmp/_aegkp", "cert/key match", timeout=30).strip()
    if "MATCH" not in match:
        raise SystemExit(f"[{host}] cert/key mismatch after install — aborting (NOT restarting)")
    run(
        c,
        f"cd {REMOTE_HY2_DIR} && "
        f"ln -s versions/{version} .current-{version} && "
        f"mv -Tf .current-{version} current",
        "activate cert pair",
        timeout=30,
    )
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
    # mtg v2 prints the fake-TLS secret in base64url (it decodes to 0xEE… + the
    # camouflage domain), NOT the hex "ee…" form — both are valid in tg://proxy.
    secret = run(c,
        "docker inspect aegis-mtg --format '{{range .Args}}{{println .}}{{end}}' "
        "2>/dev/null | grep -E '^(ee[0-9a-f]+|[A-Za-z0-9_-]{30,})$' | head -1 || true",
        "existing mtg secret", timeout=30).strip()
    if not secret:
        secret = run(c, f"docker run --rm {MTPROXY_IMAGE} generate-secret {fake_tls_domain}",
                     "generate mtg secret", timeout=120).strip().splitlines()[-1].strip()
    if not secret or len(secret) < 20 or " " in secret:
        raise SystemExit(f"[{host}] mtg generate-secret returned an invalid secret: {secret!r}")
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
    main_c: paramiko.SSHClient,
    host: str,
    obfs_password: str,
    hy2_sni: str,
    *,
    force_enable: bool = False,
) -> None:
    """D) Mirror this node's Hy2 + tcp-inbound config into its bot DB row.

    Matches the servers row by host=<node IP> and writes tcp_port, hy2_port
    (= UDP :443), and hy2_sni (the LE cert domain). The hop range + up/down + obfs
    are NULLed — Hy2 is now bare QUIC on :443. A previously provisioned node with
    ``hy2_enabled=0`` stays disabled during a routine full-stack re-provision;
    this preserves an operator quarantine such as a path-filtered node. A fresh
    row (no hy2_port yet) is enabled, while the dedicated ``--enable-hy2`` path
    passes ``force_enable=True`` to restore a quarantined node deliberately.
    The obfs_password arg is "" and unused (hy2_capable no longer needs it).
    """
    enabled_assignment = (
        "hy2_enabled=1"
        if force_enable
        else "hy2_enabled=CASE WHEN hy2_port IS NULL THEN 1 ELSE hy2_enabled END"
    )
    py = (
        "import asyncio\n"
        "from sqlalchemy import text\n"
        "from src.core.database import async_session_maker\n"
        f"HOST = {host!r}\n"
        f"OBFS = {obfs_password!r}\n"
        f"SNI = {hy2_sni!r}\n"
        f"TCP_PORT = {XRAY_TCP_PORT}\n"
        f"HY2_PORT = {HY2_LISTEN_PORT}\n"
        "async def q():\n"
        "    async with async_session_maker() as s:\n"
        "        res = await s.execute(text('UPDATE servers SET '\n"
        f"            'tcp_port=:tcp, {enabled_assignment}, hy2_port=:hp, '\n"
        "            'hy2_hop_start=NULL, hy2_hop_end=NULL, hy2_up=NULL, hy2_down=NULL, '\n"
        "            'hy2_obfs_password=:obfs, hy2_sni=:sni WHERE host=:h'),\n"
        "            {'tcp': TCP_PORT, 'hp': HY2_PORT, 'obfs': OBFS, 'sni': SNI, 'h': HOST})\n"
        "        await s.commit()\n"
        "        print('rows updated:', res.rowcount)\n"
        "        r = await s.execute(text('SELECT id, name, host, tcp_port, '\n"
        "            'hy2_enabled, hy2_port, hy2_sni FROM '\n"
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
    # A provisioned node runs 2 vless inbounds (primary + tcp); gRPC is dropped. Warn
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


def enable_hy2(
    main_c: paramiko.SSHClient,
    node_c: paramiko.SSHClient,
    *,
    host: str,
    geo_sni: str,
) -> None:
    """Enable Hy2 on an existing split node without restarting Xray."""
    cert_b64, key_b64, hy2_domain = renew_hy2_cert_acme(main_c)
    services = run(
        node_c,
        "cd /root/aegis/deploy/vps && "
        "docker compose --profile hysteria config --services | "
        "grep -x hysteria",
        "verify hysteria compose service",
        timeout=60,
    )
    if "hysteria" not in services.splitlines():
        raise SystemExit(f"[{host}] compose has no hysteria service")

    stats_secret = provision_hy2_agent_env(node_c, host)
    provision_firewall(node_c, host)
    obfs_password = provision_hysteria(
        node_c,
        host,
        geo_sni,
        stats_secret,
    )
    install_hy2_cert(node_c, host, cert_b64, key_b64)
    update_agent(node_c, host)
    verify_stack(node_c, host)
    sync_bot_db_hy2(
        main_c,
        host,
        obfs_password,
        hy2_domain,
        force_enable=True,
    )
    print(f"  [{host}] Hy2 enabled without restarting Xray ✓")


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
        "--verify-runtime-pins",
        action="store_true",
        help="Validate runtime-versions.env and optionally --xray-archive, then exit.",
    )
    p.add_argument(
        "--runtime-versions",
        type=Path,
        default=RUNTIME_VERSIONS_FILE,
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--xray-archive",
        type=Path,
        default=None,
        help="Optional downloaded Xray archive to checksum during pin verification.",
    )
    control_rollout = p.add_mutually_exclusive_group()
    control_rollout.add_argument(
        "--promote-pull",
        action="store_true",
        help="Canary one node: enable authoritative outbound apply, wait for a "
             "fresh matching generation/digest acknowledgement, then bind the "
             "Agent API to loopback and close TCP/8444 without restarting Xray.",
    )
    control_rollout.add_argument(
        "--rollback-observe",
        action="store_true",
        help="Return one node to observe/push and allow TCP/8444 only from the "
             "fixed --main-host IP. Xray remains untouched.",
    )
    p.add_argument(
        "--server-id",
        type=int,
        help="Bot database server id for --promote-pull/--rollback-observe.",
    )
    p.add_argument(
        "--promotion-timeout",
        type=int,
        default=180,
        help="Seconds to wait for an exact apply acknowledgement (default 180).",
    )
    p.add_argument(
        "--patch-dns", action="store_true",
        help="Rewrite the DNS block of the LIVE xray config on --node targets from "
             "agent/template.json, keeping every client, then restart xray. Needed "
             "because a node in service never re-renders the template, so shipping a "
             "new template.json alone changes nothing. Costs a brief session drop "
             "on each patched node.",
    )
    p.add_argument(
        "--patch-stability",
        action="store_true",
        help="Atomically apply the tested policy/routing/DNS stability profile "
             "to each live Xray config, preserving clients and Reality identity. "
             "Validates a candidate and keeps a rollback backup before one brief "
             "Xray restart.",
    )
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
        help="Read the managed Hy2 Let's Encrypt cert from the control host "
             "(Caddy preferred, legacy acme.sh fallback), install it on every "
             "--node, and restart Hysteria.",
    )
    p.add_argument(
        "--enable-hy2",
        action="store_true",
        help="Enable Hysteria2 UDP/443 on existing split nodes with the control "
             "host's managed DuckDNS certificate. Recreates only Agent and "
             "Hysteria; Xray is not restarted.",
    )
    p.add_argument(
        "--split-migrate", action="store_true",
        help="One-time: migrate --node targets from the single vpn container to "
             "the split xray+agent topology (zero-drop code deploys afterwards). "
             "Uploads docker-compose.yml + agent sources. One brief xray blip.",
    )
    p.add_argument(
        "--provision-stack", action="store_true",
        help="Roll the full canary stack (xray tcp+VISION :2053 + "
             "Hysteria2) onto --node targets and sync the bot DB. Idempotent + "
             "re-runnable. Requires --main-password (for the DB write).",
    )
    p.add_argument(
        "--geo-sni", default=None, metavar="NAME",
        help="Override the Hysteria2 self-signed cert CN / geo-SNI for "
             "--provision-stack/--enable-hy2 (e.g. csc.fi, "
             "www.chalmers.se, uio.no, aegean.gr, www.osaka-u.ac.jp).",
    )
    p.add_argument(
        "--with-mtproxy", action="store_true",
        help="(reserved) Also run the future MTProto proxy hook during "
             "--provision-stack. Currently a no-op stub.",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not any((args.bot, args.nodes, args.patch_dns, args.patch_stability,
                args.mtproxy, args.set_network,
                args.split_migrate, args.provision_stack, args.renew_hy2_cert,
                args.enable_hy2,
                args.promote_pull, args.rollback_observe,
                args.verify_runtime_pins)):
        raise SystemExit(
            "Specify --bot, --nodes, --patch-dns, --patch-stability, --mtproxy, "
            "--set-network, --split-migrate, --provision-stack, "
            "--renew-hy2-cert, --enable-hy2, --promote-pull, and/or "
            "--rollback-observe, --verify-runtime-pins"
        )
    if args.verify_runtime_pins:
        verify_runtime_pins(args.runtime_versions, args.xray_archive)
        print("Runtime pins verified.")
        if not any((args.bot, args.nodes, args.patch_dns, args.patch_stability,
                    args.mtproxy, args.set_network, args.split_migrate,
                    args.provision_stack, args.renew_hy2_cert, args.enable_hy2,
                    args.promote_pull, args.rollback_observe)):
            return
    if args.promote_pull or args.rollback_observe:
        if not args.main_password or args.server_id is None:
            raise SystemExit(
                "control rollout requires --main-password and --server-id"
            )
        if not args.nodes_list or len(args.nodes_list) != 1:
            raise SystemExit(
                "control rollout requires exactly one --node IP:PASSWORD"
            )
        if args.promotion_timeout < 1:
            raise SystemExit("--promotion-timeout must be positive")
        try:
            fixed_control_ip = ip_address(args.main_host)
        except ValueError as exc:
            raise SystemExit(
                "--main-host must be the fixed IPv4 control-server address"
            ) from exc
        if not isinstance(fixed_control_ip, IPv4Address):
            raise SystemExit("--main-host must be a fixed IPv4 address")
    if args.renew_hy2_cert and (not args.main_password or not args.nodes_list):
        raise SystemExit("--renew-hy2-cert requires --main-password (ACME host) "
                         "and at least one --node IP:PASSWORD to install onto")
    if args.enable_hy2 and (
        not args.main_password or not args.nodes_list or not args.geo_sni
    ):
        raise SystemExit(
            "--enable-hy2 requires --main-password, at least one "
            "--node IP:PASSWORD, and --geo-sni"
        )
    if args.bot and not args.main_password:
        raise SystemExit("--bot requires --main-password")
    if args.provision_stack and not args.nodes_list:
        raise SystemExit("--provision-stack requires at least one --node IP:PASSWORD")
    if args.provision_stack and not args.main_password:
        raise SystemExit("--provision-stack requires --main-password (to update the bot DB)")
    if (
        args.nodes
        or args.mtproxy
        or args.patch_dns
        or args.patch_stability
    ) and not args.nodes_list:
        raise SystemExit(
            "--nodes/--mtproxy/--patch-dns/--patch-stability requires at least "
            "one --node"
        )
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

    if args.promote_pull or args.rollback_observe:
        node_spec = args.nodes_list[0]
        parts = node_spec.split(":")
        if len(parts) < 2:
            raise SystemExit(
                f"Bad --node format (expected IP:PASSWORD): {node_spec}"
            )
        node_ip, node_password = parts[0], ":".join(parts[1:])
        print(f"[control-rollout {node_ip}] connecting to control and node hosts…")
        main_control = connect(args.main_host, args.main_password)
        node_control = connect(node_ip, node_password)
        try:
            if args.promote_pull:
                promote_pull(
                    main_control,
                    node_control,
                    server_id=args.server_id,
                    control_server_ip=str(fixed_control_ip),
                    timeout_seconds=args.promotion_timeout,
                )
            else:
                rollback_observe(
                    main_control,
                    node_control,
                    server_id=args.server_id,
                    control_server_ip=str(fixed_control_ip),
                )
        finally:
            node_control.close()
            main_control.close()

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

    if args.enable_hy2:
        print(f"[enable-hy2] connecting to control host {args.main_host}…")
        main_hy2 = connect(args.main_host, args.main_password)
        try:
            for node_str in args.nodes_list:
                parts = node_str.split(":")
                if len(parts) < 2:
                    raise SystemExit(
                        f"Bad --node format (expected IP:PASSWORD): {node_str}"
                    )
                ip, password = parts[0], ":".join(parts[1:])
                print(f"[enable-hy2 {ip}] connecting…")
                node_hy2 = connect(ip, password)
                try:
                    enable_hy2(
                        main_hy2,
                        node_hy2,
                        host=ip,
                        geo_sni=args.geo_sni,
                    )
                finally:
                    node_hy2.close()
        finally:
            main_hy2.close()

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

    if args.nodes or args.mtproxy or args.patch_dns or args.patch_stability:
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
                if args.patch_dns:
                    patch_node_dns(c, ip)
                if args.patch_stability:
                    patch_node_stability(c, ip)
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
