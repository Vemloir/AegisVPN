#!/bin/bash
set -e

ENV_FILE="/data/agent.env"
XRAY_CONFIG="${XRAY_CONFIG_PATH:-/etc/xray/config.json}"
XRAY_RUN_MODE="${XRAY_RUN_MODE:-external}"
TEMPLATE_FILE="/app/template.json"

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
    REALITY_DEST=${REALITY_DEST:-"gateway.icloud.com:443"}
    REALITY_SERVER_NAME=${REALITY_SERVER_NAME:-"gateway.icloud.com"}
    HOST_IP=${HOST_IP:-$(curl -s https://api.ipify.org || echo "127.0.0.1")}
XHTTP_PATH=${XHTTP_PATH:-"/"}
XHTTP_MODE=${XHTTP_MODE:-"packet-up"}
REALITY_TCP_DEST=${REALITY_TCP_DEST:-$REALITY_DEST}
REALITY_TCP_SERVER_NAME=${REALITY_TCP_SERVER_NAME:-$REALITY_SERVER_NAME}
TCP_KEYS=$(xray x25519 2>&1)
PRIVATE_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^PrivateKey:|^Private key:/{print $2; exit}')
PUBLIC_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^PublicKey:|^Public key:/{print $2; exit}')
if [ -z "$PUBLIC_KEY_TCP" ]; then
    PUBLIC_KEY_TCP=$(printf '%s\n' "$TCP_KEYS" | awk -F': ' '/^Password \(PublicKey\):|^Password:/{print $2; exit}')
fi
SHORT_ID_TCP=$(python -c "import secrets; print(secrets.token_hex(8))")

    cat <<EOF > "$ENV_FILE"
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
EOF

    echo "=== AGENT TOKEN: $AGENT_TOKEN ==="
    echo "=== PUBLIC KEY: $PUBLIC_KEY ==="
    echo "=== SHORT ID: $SHORT_ID ==="
fi

set -a
source "$ENV_FILE"
set +a

mkdir -p "$(dirname "$XRAY_CONFIG")"

# Keep existing config to preserve current clients across restarts.
if [ ! -f "$XRAY_CONFIG" ]; then
    sed -e "s/\"\$XRAY_PORT\"/$XRAY_PORT/g" \
        -e "s/\$REALITY_DEST/$REALITY_DEST/g" \
        -e "s/\$REALITY_SERVER_NAME/$REALITY_SERVER_NAME/g" \
        -e "s/\$PRIVATE_KEY/$PRIVATE_KEY/g" \
        -e "s/\$SHORT_ID/$SHORT_ID/g" \
        "$TEMPLATE_FILE" > "$XRAY_CONFIG"
fi

export XRAY_CONFIG

python - <<'PY'
import copy
import json
import os

config_path = os.environ.get("XRAY_CONFIG") or os.environ.get("XRAY_CONFIG_PATH") or "/etc/xray/config.json"
network = os.environ.get("XRAY_NETWORK", "tcp").strip().lower() or "tcp"
xhttp_path = os.environ.get("XHTTP_PATH", "/").strip() or "/"
xhttp_mode = os.environ.get("XHTTP_MODE", "packet-up").strip() or "packet-up"
tcp_port = (os.environ.get("XRAY_TCP_PORT") or "").strip()

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
            client_map[client_id] = dict(client)


def build_inbound(port: int, transport: str, tag: str) -> dict:
    inbound = copy.deepcopy(base_inbound)
    inbound["port"] = int(port)
    inbound["protocol"] = "vless"
    inbound["tag"] = tag  # required for live xray api adu/rmu

    settings = inbound.setdefault("settings", {})
    settings["decryption"] = "none"
    clients = []
    for client in client_map.values():
        client_copy = dict(client)
        if transport == "xhttp":
            client_copy.pop("flow", None)
        else:
            client_copy["flow"] = "xtls-rprx-vision"
        clients.append(client_copy)
    settings["clients"] = clients

    stream = inbound.setdefault("streamSettings", {})
    stream["network"] = transport
    stream["security"] = "reality"
    reality = stream.setdefault("realitySettings", {})
    reality_dest = os.environ["REALITY_DEST"]
    reality_server_name = os.environ["REALITY_SERVER_NAME"]
    reality_private_key = os.environ["PRIVATE_KEY"]
    reality_short_id = os.environ["SHORT_ID"]
    if transport == "tcp":
        reality_dest = os.environ.get("REALITY_TCP_DEST") or reality_dest
        reality_server_name = os.environ.get("REALITY_TCP_SERVER_NAME") or reality_server_name
        reality_private_key = os.environ.get("PRIVATE_KEY_TCP") or reality_private_key
        reality_short_id = os.environ.get("SHORT_ID_TCP") or reality_short_id
    server_names = [name.strip() for name in reality_server_name.split(",") if name.strip()]
    if not server_names:
        server_names = [reality_server_name]
    reality["show"] = False
    reality["dest"] = reality_dest
    reality["xver"] = 0
    reality["serverNames"] = server_names
    reality["privateKey"] = reality_private_key
    reality["shortIds"] = [reality_short_id]

    if transport == "xhttp":
        stream["xhttpSettings"] = {
            "path": xhttp_path,
            "mode": xhttp_mode,
        }
    else:
        stream.pop("xhttpSettings", None)

    return inbound


primary_port = int(os.environ["XRAY_PORT"])
new_inbounds = [build_inbound(primary_port, network, "vless-in")]
if tcp_port:
    tcp_port_int = int(tcp_port)
    if tcp_port_int > 0 and tcp_port_int != primary_port:
        new_inbounds.append(build_inbound(tcp_port_int, "tcp", "vless-in-tcp"))

config["inbounds"] = new_inbounds + other_inbounds


def ensure_warp(cfg: dict) -> None:
    """Idempotently wire a Cloudflare WARP (WireGuard) outbound + an AI-domain
    routing rule into the config when WARP_SECRET_KEY is present in the env.

    Lives here (not in template.json) so the creds never touch the repo and so
    it re-applies on every restart: the inbounds are rebuilt each boot while
    outbounds/routing are otherwise preserved as-is, which would silently drop
    a WARP block added out-of-band.

    Scope is deliberately narrow — ONLY Google Gemini. Other AI services
    (OpenAI, Anthropic, …) work fine directly from our VPS ranges, so sending
    them through WARP would only add a needless hop. Gemini is the one that
    refuses our egress IPs, so just its domains go through Cloudflare's WARP.
    """
    secret = os.environ.get("WARP_SECRET_KEY", "").strip()
    if not secret:
        return

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

    outbounds = cfg.setdefault("outbounds", [])
    # Replace any prior warp block so creds/endpoint stay current, else insert
    # just before the trailing blackhole/last outbound.
    outbounds[:] = [o for o in outbounds if o.get("tag") != "warp"]
    insert_at = len(outbounds) - 1 if outbounds else 0
    outbounds.insert(max(insert_at, 0), warp_outbound)

    rules = cfg.setdefault("routing", {}).setdefault("rules", [])
    # Drop any prior warp rule and re-add, so a narrowed/updated domain set
    # takes effect on restart instead of being frozen at first-write.
    rules[:] = [r for r in rules if r.get("outboundTag") != "warp"]
    gemini_rule = {
        "type": "field",
        "domain": [
            "geosite:google-gemini",
            "domain:gemini.google.com",
            "domain:generativelanguage.googleapis.com",
            "domain:aistudio.google.com",
            "domain:alkalimakersuite-pa.clients6.google.com",
            "domain:labs.google",
        ],
        "outboundTag": "warp",
    }
    api_idx = next((i for i, r in enumerate(rules) if r.get("inboundTag") == ["api"]), -1)
    rules.insert(api_idx + 1, gemini_rule)


ensure_warp(config)

with open(config_path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
PY

if [ "$XRAY_RUN_MODE" = "internal" ]; then
    run_xray_loop() {
        while true; do
            echo "Starting Xray..."
            set +e
            xray run -c "$XRAY_CONFIG"
            EXIT_CODE=$?
            set -e
            echo "Xray exited with code $EXIT_CODE, restarting in 1s..."
            sleep 1
        done
    }

    run_xray_loop &
    XRAY_LOOP_PID=$!

    echo "Starting Agent API..."
    uvicorn app.main:app --host 0.0.0.0 --port 8444 &
    API_PID=$!

    trap 'kill $API_PID $XRAY_LOOP_PID 2>/dev/null || true; pkill -TERM xray 2>/dev/null || true' INT TERM

    wait "$API_PID"
    API_EXIT_CODE=$?

    kill "$XRAY_LOOP_PID" 2>/dev/null || true
    pkill -TERM xray 2>/dev/null || true
    wait "$XRAY_LOOP_PID" 2>/dev/null || true
    exit "$API_EXIT_CODE"
fi

pkill -HUP xray || true

echo "Starting Agent API..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8444
