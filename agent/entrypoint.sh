#!/bin/bash
set -e
umask 077

ENV_FILE="/data/agent.env"
XRAY_CONFIG="${XRAY_CONFIG_PATH:-/etc/xray/config.json}"
XRAY_RUN_MODE="${XRAY_RUN_MODE:-external}"
AGENT_BIND_HOST="${AGENT_BIND_HOST:-0.0.0.0}"
TEMPLATE_FILE="/app/template.json"

# --- split topology: the xray container runs ONLY the data plane -------------
# In the split deployment the agent container owns config generation; this role
# just runs xray against the shared on-disk config. It must NOT init keys or
# rebuild the config (that would race the agent), so short-circuit before all of
# that. On a cold start the config may not be written yet — wait briefly for the
# agent to produce it (existing nodes already have it on the volume).
if [ "$XRAY_RUN_MODE" = "xray-only" ]; then
    echo "xray-only: waiting for $XRAY_CONFIG …"
    for _ in $(seq 1 120); do
        [ -f "$XRAY_CONFIG" ] && break
        sleep 0.5
    done
    exec xray run -c "$XRAY_CONFIG"
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Initializing new agent..."

    KEYS=$(xray x25519 2>&1)
    PRIVATE_KEY=$(printf '%s\n' "$KEYS" | awk -F': ' '/^PrivateKey:|^Private key:/{print $2; exit}')
    PUBLIC_KEY=$(printf '%s\n' "$KEYS" | awk -F': ' '/^PublicKey:|^Public key:/{print $2; exit}')

    # Newer Xray versions renamed the client-side public key field to "Password".
    if [ -z "$PUBLIC_KEY" ]; then
        PUBLIC_KEY=$(printf '%s\n' "$KEYS" | awk -F': ' '/^Password \(PublicKey\):|^Password:/{print $2; exit}')
    fi

    if [ -z "$PRIVATE_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
        echo "Failed to parse xray x25519 output:"
        echo "$KEYS"
        exit 1
    fi

    AGENT_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
    SHORT_ID=$(python -c "import secrets; print(secrets.token_hex(8))")

    XRAY_PORT=${XRAY_PORT:-443}
    XRAY_TCP_PORT=${XRAY_TCP_PORT:-9445}
    XRAY_NETWORK=${XRAY_NETWORK:-tcp}
    # No gateway.icloud.com default — a node must be given a real geo-matched SNI
    # (add_server.py --reality-server-name / update.py --provision-stack --geo-sni).
    # An empty value surfaces a missing-SNI misconfig instead of silently fronting
    # an implausible Apple domain (which РКН active-probes).
    REALITY_DEST=${REALITY_DEST:-""}
    REALITY_SERVER_NAME=${REALITY_SERVER_NAME:-""}
    HOST_IP=${HOST_IP:-$(curl -s https://api.ipify.org || echo "127.0.0.1")}
XHTTP_PATH=${XHTTP_PATH:-"/"}
# "auto" lets the SERVER accept packet-up AND stream-up/stream-one clients at
# once (xray hub.go is strict for any explicit mode, 400s the others), and lets
# a client over direct REALITY resolve to stream-one (single full-duplex stream,
# less overhead than packet-up's many POSTs — packet-up only helps behind a CDN).
XHTTP_MODE=${XHTTP_MODE:-"auto"}
XRAY_GRPC_PORT=${XRAY_GRPC_PORT:-}
XRAY_GRPC_SERVICE=${XRAY_GRPC_SERVICE:-"grpc"}
XRAY_CONN_IDLE=${XRAY_CONN_IDLE:-300}
REALITY_TCP_DEST=${REALITY_TCP_DEST:-$REALITY_DEST}
REALITY_TCP_SERVER_NAME=${REALITY_TCP_SERVER_NAME:-$REALITY_SERVER_NAME}
TCP_KEYS=$(xray x25519 2>&1)
PRIVATE_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^PrivateKey:|^Private key:/{print $2; exit}')
PUBLIC_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^PublicKey:|^Public key:/{print $2; exit}')
if [ -z "$PUBLIC_KEY_TCP" ]; then
    PUBLIC_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^Password \(PublicKey\):|^Password:/{print $2; exit}')
fi
SHORT_ID_TCP=$(python -c "import secrets; print(secrets.token_hex(8))")

    mkdir -p "$(dirname "$ENV_FILE")"
    ENV_TEMP=$(mktemp "$(dirname "$ENV_FILE")/.agent.env.XXXXXX")
    trap 'rm -f "$ENV_TEMP"' EXIT
    cat <<EOF > "$ENV_TEMP"
AGENT_TOKEN=$AGENT_TOKEN
SHORT_ID=$SHORT_ID
PRIVATE_KEY=$PRIVATE_KEY
PUBLIC_KEY=$PUBLIC_KEY
SHORT_ID_TCP=$SHORT_ID_TCP
PRIVATE_KEY_TCP=$PRIVATE_KEY_TCP
PUBLIC_KEY_TCP=$PUBLIC_KEY_TCP
XRAY_PORT=$XRAY_PORT
XRAY_TCP_PORT=$XRAY_TCP_PORT
XRAY_NETWORK=$XRAY_NETWORK
REALITY_DEST=$REALITY_DEST
REALITY_SERVER_NAME=$REALITY_SERVER_NAME
REALITY_TCP_DEST=$REALITY_TCP_DEST
REALITY_TCP_SERVER_NAME=$REALITY_TCP_SERVER_NAME
HOST_IP=$HOST_IP
XHTTP_PATH=$XHTTP_PATH
XHTTP_MODE=$XHTTP_MODE
XRAY_GRPC_PORT=$XRAY_GRPC_PORT
XRAY_GRPC_SERVICE=$XRAY_GRPC_SERVICE
XRAY_CONN_IDLE=$XRAY_CONN_IDLE
EOF
    chmod 600 "$ENV_TEMP"
    sync -f "$ENV_TEMP"
    mv -f "$ENV_TEMP" "$ENV_FILE"
    sync -f "$(dirname "$ENV_FILE")"
    trap - EXIT

    echo "=== PUBLIC KEY: $PUBLIC_KEY ==="
    echo "=== SHORT ID: $SHORT_ID ==="
fi

set -a
source "$ENV_FILE"
set +a

mkdir -p "$(dirname "$XRAY_CONFIG")"

# Keep existing config to preserve current clients across restarts.
if [ ! -f "$XRAY_CONFIG" ]; then
    CONFIG_TEMP=$(mktemp "$(dirname "$XRAY_CONFIG")/.config.json.XXXXXX")
    trap 'rm -f "$CONFIG_TEMP"' EXIT
    sed -e "s/\"\$XRAY_PORT\"/$XRAY_PORT/g" \
        -e "s/\$REALITY_DEST/$REALITY_DEST/g" \
        -e "s/\$REALITY_SERVER_NAME/$REALITY_SERVER_NAME/g" \
        -e "s/\$PRIVATE_KEY/$PRIVATE_KEY/g" \
        -e "s/\$SHORT_ID/$SHORT_ID/g" \
        "$TEMPLATE_FILE" > "$CONFIG_TEMP"
    chmod 600 "$CONFIG_TEMP"
    sync -f "$CONFIG_TEMP"
    mv -f "$CONFIG_TEMP" "$XRAY_CONFIG"
    sync -f "$(dirname "$XRAY_CONFIG")"
    trap - EXIT
fi

export XRAY_CONFIG

python - <<'PY'
import copy
import json
import os
import tempfile

config_path = os.environ.get("XRAY_CONFIG") or os.environ.get("XRAY_CONFIG_PATH") or "/etc/xray/config.json"
network = os.environ.get("XRAY_NETWORK", "tcp").strip().lower() or "tcp"
xhttp_path = os.environ.get("XHTTP_PATH", "/").strip() or "/"
xhttp_mode = os.environ.get("XHTTP_MODE", "auto").strip() or "auto"
tcp_port = (os.environ.get("XRAY_TCP_PORT") or "").strip()
grpc_port = (os.environ.get("XRAY_GRPC_PORT") or "").strip()
grpc_service = os.environ.get("XRAY_GRPC_SERVICE", "grpc").strip() or "grpc"


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default


# connIdle reaps a connection only after this many seconds with ZERO bytes in
# either direction; the timer resets on every byte, so active tunnels are never
# cut — it just frees ghost sessions (e.g. a half-open socket left behind after
# a client roamed Wi-Fi<->cellular) faster, which also releases conn-limit slots.
conn_idle = _int_env("XRAY_CONN_IDLE", 300)
# TCP keepalive on the inbound socket detects a dead/half-open peer at the kernel
# level (idle a bit under connIdle so it can probe before connIdle reaps).
keepalive_idle = _int_env("XRAY_KEEPALIVE_IDLE", 20)
keepalive_interval = _int_env("XRAY_KEEPALIVE_INTERVAL", 10)

with open(config_path, "r", encoding="utf-8") as fh:
    config = json.load(fh)

existing_vless = [inbound for inbound in config.get("inbounds", []) if inbound.get("protocol") == "vless"]
other_inbounds = [inbound for inbound in config.get("inbounds", []) if inbound.get("protocol") != "vless"]
base_inbound = copy.deepcopy(existing_vless[0]) if existing_vless else {
    "protocol": "vless",
    "settings": {"clients": [], "decryption": "none"},
    "streamSettings": {"security": "reality", "realitySettings": {}},
}

client_map = {}
for inbound in existing_vless:
    for client in inbound.get("settings", {}).get("clients", []):
        client_id = client.get("id")
        if client_id:
            record = dict(client)
            # Normalize to a flow-less id+email here; build_inbound re-applies the
            # per-inbound flow (vision on tcp, none on xhttp/grpc).
            record.pop("flow", None)
            # The CDN inbound's copy carries "_cdn" (see build_cdn_inbound); keep
            # the subscriber's own email so the other inbounds never inherit it.
            if isinstance(record.get("email"), str):
                record["email"] = record["email"].removesuffix("_cdn")
            client_map[client_id] = record


def build_inbound(port: int, transport: str, tag: str) -> dict:
    inbound = copy.deepcopy(base_inbound)
    inbound["port"] = int(port)
    inbound["protocol"] = "vless"
    inbound["tag"] = tag  # required for live xray api adu/rmu

    settings = inbound.setdefault("settings", {})
    settings["decryption"] = "none"
    # Clients are shared across all inbounds (same id+email), but the flow is set
    # PER INBOUND: tcp/REALITY carries the vision flow (xtls-rprx-vision), while
    # xhttp/grpc stay flow-less. A node shares one reality keypair across
    # inbounds; only this per-client flow differs.
    clients = []
    for client in client_map.values():
        client_copy = dict(client)
        client_copy.pop("flow", None)
        if transport == "tcp":
            client_copy["flow"] = "xtls-rprx-vision"
        clients.append(client_copy)
    settings["clients"] = clients

    stream = inbound.setdefault("streamSettings", {})
    stream["network"] = transport
    stream["security"] = "reality"
    reality = stream.setdefault("realitySettings", {})
    # SINGLE shared reality keypair for every transport — no *_TCP variant.
    reality_dest = os.environ["REALITY_DEST"]
    reality_server_name = os.environ["REALITY_SERVER_NAME"]
    reality_private_key = os.environ["PRIVATE_KEY"]
    reality_short_id = os.environ["SHORT_ID"]
    server_names = [name.strip() for name in reality_server_name.split(",") if name.strip()]
    if not server_names:
        server_names = [reality_server_name]
    reality["show"] = False
    reality["dest"] = reality_dest
    reality["xver"] = 0
    reality["serverNames"] = server_names
    reality["privateKey"] = reality_private_key
    reality["shortIds"] = [reality_short_id]
    # Xray >= 26.7 refuses REALITY clients older than v26.3.27 unless told
    # otherwise. Subscribers run many cores (sing-box, mihomo, older Xray), so
    # accept every client version as before; REALITY_MIN_CLIENT_VER opts in.
    reality["minClientVer"] = (os.environ.get("REALITY_MIN_CLIENT_VER") or "").strip() or "0.0.0"

    if transport == "xhttp":
        stream["xhttpSettings"] = {
            "path": xhttp_path,
            "mode": xhttp_mode,
        }
    else:
        stream.pop("xhttpSettings", None)
    if transport == "grpc":
        stream["grpcSettings"] = {"serviceName": grpc_service}
    else:
        stream.pop("grpcSettings", None)

    inbound["sniffing"] = {"enabled": True, "destOverride": ["http", "tls"]}

    if keepalive_idle > 0:
        sockopt = stream.setdefault("sockopt", {})
        sockopt["tcpKeepAliveIdle"] = keepalive_idle
        sockopt["tcpKeepAliveInterval"] = keepalive_interval
    else:
        stream.pop("sockopt", None)

    return inbound


primary_port = int(os.environ["XRAY_PORT"])
new_inbounds = [build_inbound(primary_port, network, "vless-in")]
if tcp_port:
    tcp_port_int = int(tcp_port)
    if tcp_port_int > 0 and tcp_port_int != primary_port:
        new_inbounds.append(build_inbound(tcp_port_int, "tcp", "vless-in-tcp"))
if grpc_port:
    grpc_port_int = int(grpc_port)
    if grpc_port_int > 0:
        new_inbounds.append(build_inbound(grpc_port_int, "grpc", "vless-in-grpc"))


# How XHTTP must look to pass a Russian CDN (CDNvideo behind Beget/Timeweb).
# Its edge forwards only file-like paths, so the session id and sequence go in
# the query instead of the path (which would end in "/<session>/<seq>"); the
# session id is not a UUID and padding rides in a cookie, not a URL-shaped
# Referer, since its anti-proxy filter keys on both. The bot's subscription
# builds clients from the same profile (bot/src/services/cdn_service.py).
CDN_XHTTP_PROFILE = {
    "sessionIDPlacement": "query",
    "sessionIDKey": "v",
    "sessionIDTable": "Base62",
    "sessionIDLength": "16-24",
    "seqPlacement": "query",
    "seqKey": "n",
    "xPaddingObfsMode": True,
    "xPaddingPlacement": "cookie",
    "xPaddingKey": "sid",
    "xPaddingMethod": "tokenish",
}


def build_cdn_inbound(port: int) -> dict | None:
    """VLESS + XHTTP behind plain TLS: the origin a CDN resource proxies to.

    A CDN terminates the client's TLS at its edge and opens its own HTTPS
    connection to us, so REALITY cannot work here: this inbound needs an
    ordinary certificate for the origin hostname. Until the control plane has
    delivered the real one, a self-signed placeholder keeps Xray bootable.
    Returns None (no inbound) if even the placeholder cannot be installed.
    """
    cert_path = os.environ.get("CDN_CERTIFICATE_PATH", "/data/cdn-cert/current/cert.pem")
    key_path = os.environ.get("CDN_PRIVATE_KEY_PATH", "/data/cdn-cert/current/key.pem")
    try:
        from app.certificate_sync import install_placeholder_certificate

        if install_placeholder_certificate(cert_path, key_path):
            print("CDN inbound: placeholder certificate installed until the real one syncs")
    except Exception as exc:  # noqa: BLE001 - boot must not die over an optional inbound
        print(f"CDN inbound disabled: no certificate ({type(exc).__name__}: {exc})")
        return None

    path = (os.environ.get("XRAY_CDN_PATH") or "/").strip() or "/"
    stream = {
        "network": "xhttp",
        "security": "tls",
        "tlsSettings": {
            "alpn": ["h2", "http/1.1"],
            "certificates": [{"certificateFile": cert_path, "keyFile": key_path}],
        },
        # "auto" accepts every client mode; through a CDN clients use packet-up.
        "xhttpSettings": {"path": path, "mode": "auto", **CDN_XHTTP_PROFILE},
    }
    if keepalive_idle > 0:
        stream["sockopt"] = {
            "tcpKeepAliveIdle": keepalive_idle,
            "tcpKeepAliveInterval": keepalive_interval,
        }
    return {
        "tag": "vless-in-cdn",
        "port": int(port),
        "protocol": "vless",
        "settings": {
            # Same clients as every other inbound, flow-less like any xhttp one,
            # under their own "_cdn" email so Xray counts CDN traffic apart.
            "clients": [
                {
                    **{k: v for k, v in client.items() if k != "flow"},
                    **({"email": client["email"] + "_cdn"} if client.get("email") else {}),
                }
                for client in client_map.values()
            ],
            "decryption": "none",
        },
        "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        "streamSettings": stream,
    }


cdn_port = (os.environ.get("XRAY_CDN_PORT") or "").strip()
if cdn_port and int(cdn_port) > 0:
    cdn_inbound = build_cdn_inbound(int(cdn_port))
    if cdn_inbound is not None:
        new_inbounds.append(cdn_inbound)

config["inbounds"] = new_inbounds + other_inbounds

# Enforce connIdle on every boot: the policy block is otherwise preserved as-is
# from the on-disk config, so nodes initialised with an older per-node value
# would keep it forever without this. Only the connIdle field is touched; handshake
# and the per-user stats flags stay whatever the config already had.
level0 = config.setdefault("policy", {}).setdefault("levels", {}).setdefault("0", {})
level0["connIdle"] = conn_idle


def ensure_warp(cfg: dict) -> None:
    """Idempotently wire a Cloudflare WARP (WireGuard) outbound + an AI-domain
    routing rule into the config when WARP_SECRET_KEY is present in the env.

    Lives here (not in template.json) so the creds never touch the repo and so
    it re-applies on every restart: the inbounds are rebuilt each boot while
    outbounds/routing are otherwise preserved as-is, which would silently drop
    a WARP block added out-of-band.

    Scope is deliberately narrow: Microsoft services that are unreliable from
    some VPS ranges. Everything else keeps the node's direct egress and avoids
    an unnecessary hop.

    Google's AI services (Gemini, AI Studio, Labs) used to go through WARP too
    and must not by default: Google places the dual-stack WARP egress in
    Russia (its IPv6 side) or the US (its IPv4 side), so through it Gemini
    flips to "not available in your country". ensure_gemini_resolver can send
    them through WARP forced to IPv4 instead (GEMINI_EGRESS=warp).
    """
    outbounds = cfg.setdefault("outbounds", [])
    existing_warp = next((outbound for outbound in outbounds if outbound.get("tag") == "warp"), None)
    secret = os.environ.get("WARP_SECRET_KEY", "").strip()
    if secret:
        addresses = [
            a for a in (
                os.environ.get("WARP_ADDR_V4", "").strip(),
                os.environ.get("WARP_ADDR_V6", "").strip(),
            ) if a
        ]
        reserved_raw = os.environ.get("WARP_RESERVED", "").strip()
        reserved = [int(x) for x in reserved_raw.split(",") if x.strip().lstrip("-").isdigit()]
        warp_outbound = {
            "tag": "warp",
            "protocol": "wireguard",
            "settings": {
                "secretKey": secret,
                "address": addresses,
                "peers": [{
                    "publicKey": os.environ.get("WARP_PEER_PUBKEY", "").strip(),
                    "endpoint": os.environ.get("WARP_ENDPOINT", "").strip() or "162.159.192.1:2408",
                }],
                "mtu": int(os.environ.get("WARP_MTU", "1280") or "1280"),
            },
        }
        if reserved:
            warp_outbound["settings"]["reserved"] = reserved
    elif existing_warp is not None:
        # Older nodes stored the per-node WARP registration only in the live
        # Xray config. Preserve that secret material while refreshing rules.
        warp_outbound = existing_warp
    else:
        return

    # Replace any prior warp block so creds/endpoint stay current, else insert
    # just before the trailing blackhole/last outbound.
    outbounds[:] = [o for o in outbounds if o.get("tag") != "warp"]
    insert_at = len(outbounds) - 1 if outbounds else 0
    outbounds.insert(max(insert_at, 0), warp_outbound)

    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    # Drop any prior warp rule and re-add, so a narrowed/updated domain set
    # takes effect on restart instead of being frozen at first-write.
    rules[:] = [r for r in rules if r.get("outboundTag") != "warp"]
    games_direct_rule = {
        "type": "field",
        "domain": [
            "geosite:category-games",
            "domain:callofduty.com",
            "domain:activision.com",
            "domain:activisionblizzard.com",
            "domain:demonware.net",
        ],
        "outboundTag": "direct",
    }
    rules[:] = [
        rule for rule in rules
        if "geosite:category-games" not in rule.get("domain", [])
    ]
    warp_domains_rule = {
        "type": "field",
        "domain": ["geosite:microsoft"],
        "outboundTag": "warp",
    }
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    # First-match routing: every game stays on the node's direct egress even
    # when its owner/service domains are also covered by geosite:microsoft.
    rules.insert(api_idx + 1, games_direct_rule)
    rules.insert(api_idx + 2, warp_domains_rule)


# Every name comss sends to its AI proxies (checked 2026-10-13): the Gemini
# page and app call several backends besides gemini.google.com, and one of
# them leaving from the node's own IP is enough to bring the error back.
GEMINI_DOMAINS = [
    "geosite:google-gemini",
    "domain:gemini.google.com",
    "domain:bard.google.com",
    "domain:generativelanguage.googleapis.com",
    "domain:robinfrontend-pa.googleapis.com",
    "domain:proactivebackend-pa.googleapis.com",
    "domain:alkalimakersuite-pa.clients6.google.com",
    "domain:waa-pa.clients6.google.com",
    "domain:signaler-pa.clients6.google.com",
    "domain:ogads-pa.clients6.google.com",
    "domain:apis.google.com",
    "domain:aistudio.google.com",
    "domain:ai.google.dev",
    "domain:cloudcode-pa.googleapis.com",
    "domain:notebooklm.google.com",
    "domain:notebooklm.google",
    "domain:labs.google",
    "domain:aisandbox-pa.googleapis.com",
    "domain:aitestkitchen.withgoogle.com",
    "domain:stitch.withgoogle.com",
    "domain:antigravity.google",
]


def ensure_gemini_resolver(cfg: dict) -> None:
    """Send Gemini out through an egress Google accepts for it.

    Gemini answers "not available in your country" from our nodes' own IPs,
    whatever country Google places them in. Two ways out, chosen per node:

    - GEMINI_EGRESS=warp: the node's WARP outbound, forced to IPv4. Google
      places WARP's IPv4 egress in the US and its IPv6 egress in Russia, so
      without the force a dual-stack tunnel flips between the two.
    - GEMINI_RESOLVER (a DoH URL): a resolver such as GeoHide or comss answers
      Gemini's names with an SNI proxy that passes TLS through untouched (the
      client still verifies Google's own certificate).
      GEMINI_RESOLVER_SKIP_IPS (comma-separated) drops dead proxies from its
      answers.

    Only Gemini's names take that way; everything else exits as before. QUIC
    to those names is blocked so browsers fall back to TCP, which both ways
    carry. With neither set, every piece added by an earlier boot is removed.
    """
    outbounds = cfg.setdefault("outbounds", [])
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    dns = cfg.setdefault("dns", {})
    servers = dns.setdefault("servers", [])
    # Recognised by a name every version of the list carried, so a boot after
    # the list changed still clears what an older one added.
    def ours(entry: dict, key: str) -> bool:
        return "domain:gemini.google.com" in entry.get(key, [])

    outbounds[:] = [o for o in outbounds if o.get("tag") != "gemini"]
    rules[:] = [r for r in rules if not ours(r, "domain")]
    servers[:] = [s for s in servers if not (isinstance(s, dict) and ours(s, "domains"))]
    warp = next((o for o in outbounds if o.get("tag") == "warp"), None)
    if warp is not None:
        warp.setdefault("settings", {}).pop("domainStrategy", None)
    for inbound in cfg.get("inbounds", []):
        sniffing = inbound.get("sniffing") or {}
        if "quic" in sniffing.get("destOverride", []) and not sniffing.get("routeOnly"):
            sniffing["destOverride"] = [d for d in sniffing["destOverride"] if d != "quic"]

    resolver = (os.environ.get("GEMINI_RESOLVER") or "").strip()
    if (os.environ.get("GEMINI_EGRESS") or "").strip().lower() == "warp" and warp is not None:
        # The one WARP session (a second one with the same key would fight it
        # for the peer slot), forced to IPv4; Microsoft's traffic on it too.
        warp["settings"]["domainStrategy"] = "ForceIPv4"
        target = "warp"
    elif resolver:
        # skipFallback: the resolver answers Gemini's names and nothing else.
        server = {"address": resolver, "domains": GEMINI_DOMAINS, "skipFallback": True}
        # Proxies of the resolver's pool that do not answer from our nodes: a
        # connection picks one of the answers at random, so a dead one would
        # fail every few page loads.
        skip = [ip.strip() for ip in (os.environ.get("GEMINI_RESOLVER_SKIP_IPS") or "").split(",") if ip.strip()]
        if skip:
            server["unexpectedIPs"] = skip
        servers.insert(0, server)
        # ForceIPv4 resolves through the DNS above; if it fails the connection
        # fails too instead of quietly leaving from the node's own address.
        outbounds.append({"tag": "gemini", "protocol": "freedom", "settings": {"domainStrategy": "ForceIPv4"}})
        target = "gemini"
    else:
        return
    for inbound in cfg.get("inbounds", []):
        sniffing = inbound.get("sniffing") or {}
        if "tls" in sniffing.get("destOverride", []) and not sniffing.get("routeOnly"):
            sniffing["destOverride"] = [*sniffing["destOverride"], "quic"]
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    rules.insert(api_idx + 1, {"type": "field", "network": "udp", "domain": GEMINI_DOMAINS, "outboundTag": "block"})
    rules.insert(api_idx + 2, {"type": "field", "domain": GEMINI_DOMAINS, "outboundTag": target})


def ensure_private_blocked(cfg: dict) -> None:
    """Idempotently keep subscriber traffic off the node's own private network.

    The node's loopback carries the unauthenticated Xray gRPC API (127.0.0.1:10085)
    and the agent; routing geoip:private to `direct` let any subscriber reach
    them through the tunnel. Block private IPs and names right after the api
    rule, ahead of every other rule, and resolve domains on demand so a public
    name that points at 127.0.0.1 or 169.254.169.254 is caught by the IP rule
    instead of slipping through as a domain. Runs on every start so existing
    nodes are migrated, not only freshly templated ones.
    """
    routing = cfg.setdefault("routing", {})
    routing["domainStrategy"] = "IPOnDemand"
    rules = routing.setdefault("rules", [])
    rules[:] = [
        rule for rule in rules
        if "geoip:private" not in rule.get("ip", []) and "geosite:private" not in rule.get("domain", [])
    ]
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    rules.insert(api_idx + 1, {"type": "field", "ip": ["geoip:private"], "outboundTag": "block"})
    rules.insert(api_idx + 2, {"type": "field", "domain": ["geosite:private"], "outboundTag": "block"})


def ensure_lb_probe(cfg: dict) -> None:
    """Route the auto-select health check to the agent's load probe.

    Clients health-check every node with GET http://<LB_PROBE_HOST>/p/<id>
    through the tunnel. That host is never resolved: this rule, placed ahead
    of the private-network block, hands it to a freedom outbound redirected to
    the probe on loopback, and to nothing else on loopback.
    """
    port = int(os.environ.get("LB_PROBE_PORT", "10086") or "0")
    host = os.environ.get("LB_PROBE_HOST", "lb.aegisvpn.org").strip()
    outbounds = cfg.setdefault("outbounds", [])
    outbounds[:] = [o for o in outbounds if o.get("tag") != "lb-probe"]
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    rules[:] = [r for r in rules if r.get("outboundTag") != "lb-probe"]
    if port <= 0 or not host:
        return
    outbounds.append({"tag": "lb-probe", "protocol": "freedom", "settings": {"redirect": f"127.0.0.1:{port}"}})
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    rules.insert(api_idx + 1, {"type": "field", "domain": [f"full:{host}"], "port": "80", "outboundTag": "lb-probe"})


def ensure_conn_limit_reachable(cfg: dict) -> None:
    """Let the conn-limit block rule actually match.

    The agent installs it with `xray api sib`, which can only APPEND a rule, and
    routing is first-match: behind a trailing catch-all `direct` rule it was
    never consulted. Drop the catch-all and keep `direct` as the first outbound,
    which Xray uses for anything no rule matched, so unmatched traffic still
    leaves directly while an appended block rule is reached first.
    """
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    rules[:] = [
        rule for rule in rules
        if not (
            rule.get("outboundTag") == "direct"
            and set(rule) <= {"type", "network", "outboundTag"}
            and rule.get("network") in ("tcp,udp", "udp,tcp")
        )
    ]
    outbounds = cfg.setdefault("outbounds", [])
    direct = [o for o in outbounds if o.get("tag") == "direct"]
    outbounds[:] = direct + [o for o in outbounds if o.get("tag") != "direct"]


def ensure_cdn_sni_split(cfg: dict) -> None:
    """Share port 443 between REALITY and the CDN origin, split by SNI.

    A CDN reaches its origin only on 80/443, and 443 belongs to REALITY. REALITY
    already hands every handshake that is not its own to `dest`; point `dest` at
    a loopback dokodemo-door instead, sniff the SNI there, and send the origin
    hostname to the CDN inbound (now loopback-only) while everything else goes
    on to the real REALITY_DEST site exactly as before, so active probes still
    see that site. Without XRAY_CDN_HOST, or without a CDN inbound, any split
    left by an earlier boot is removed and REALITY keeps its direct `dest`.
    """
    inbounds = cfg.setdefault("inbounds", [])
    outbounds = cfg.setdefault("outbounds", [])
    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    inbounds[:] = [i for i in inbounds if i.get("tag") != "reality-split"]
    outbounds[:] = [o for o in outbounds if o.get("tag") != "cdn-origin"]
    rules[:] = [r for r in rules if r.get("inboundTag") != ["reality-split"]]

    host = (os.environ.get("XRAY_CDN_HOST") or "").strip().lower()
    cdn = next((i for i in inbounds if i.get("tag") == "vless-in-cdn"), None)
    if not host or cdn is None:
        return
    split_port = _int_env("XRAY_CDN_SPLIT_PORT", 10443)
    dest_host, _, dest_port = os.environ["REALITY_DEST"].rpartition(":")

    cdn["listen"] = "127.0.0.1"
    for inbound in inbounds:
        reality = inbound.get("streamSettings", {}).get("realitySettings")
        if reality is not None:
            reality["dest"] = f"127.0.0.1:{split_port}"
    inbounds.append({
        "tag": "reality-split",
        "listen": "127.0.0.1",
        "port": split_port,
        "protocol": "dokodemo-door",
        "settings": {"address": dest_host, "port": int(dest_port or 443), "network": "tcp"},
        # routeOnly: the SNI picks the route, the destination stays REALITY_DEST.
        "sniffing": {"enabled": True, "destOverride": ["tls"], "routeOnly": True},
    })
    outbounds.append({"tag": "cdn-origin", "protocol": "freedom", "settings": {"redirect": f"127.0.0.1:{cdn['port']}"}})
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    rules.insert(api_idx + 1, {"type": "field", "inboundTag": ["reality-split"], "domain": [f"full:{host}"], "outboundTag": "cdn-origin"})
    rules.insert(api_idx + 2, {"type": "field", "inboundTag": ["reality-split"], "outboundTag": "direct"})


ensure_warp(config)
ensure_gemini_resolver(config)
ensure_private_blocked(config)
ensure_lb_probe(config)
ensure_conn_limit_reachable(config)
ensure_cdn_sni_split(config)

config_directory = os.path.dirname(config_path) or "."
descriptor, temporary_path = tempfile.mkstemp(prefix=".config-", dir=config_directory)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, config_path)
    directory = os.open(config_directory, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary_path)
    except FileNotFoundError:
        pass
PY

if [ "$XRAY_RUN_MODE" = "internal" ]; then
    run_xray_loop() {
        while true; do
            echo "Starting Xray..."
            set +e
            xray run -c "$XRAY_CONFIG"
            set -e
            echo "Xray exited, restarting in 1s..."
            sleep 1
        done
    }

    run_uvicorn_loop() {
        while true; do
            echo "Starting Agent API..."
            set +e
            uvicorn app.main:app --host "$AGENT_BIND_HOST" --port 8444
            set -e
            echo "uvicorn exited, restarting in 1s..."
            sleep 1
        done
    }

    run_xray_loop &
    XRAY_LOOP_PID=$!
    run_uvicorn_loop &
    API_LOOP_PID=$!

    trap 'kill "$XRAY_LOOP_PID" "$API_LOOP_PID" 2>/dev/null; pkill -TERM xray uvicorn 2>/dev/null; exit 0' INT TERM

    # Wait indefinitely; SIGTERM from docker stop will trigger the trap.
    while true; do sleep 60; done
fi

# Legacy single-container 'external' mode shares this container with xray, so a
# config rebuild needs a HUP to reload it. In the split 'agent-only' role xray
# lives in its OWN container and must survive agent restarts untouched (that is
# the whole point — zero-drop code deploys), so never signal it here; transport
# changes restart the xray container explicitly via the deploy tooling.
if [ "$XRAY_RUN_MODE" != "agent-only" ]; then
    pkill -HUP xray || true
fi

echo "Starting Agent API..."
exec uvicorn app.main:app --host "$AGENT_BIND_HOST" --port 8444
