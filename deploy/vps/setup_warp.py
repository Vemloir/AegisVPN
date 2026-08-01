#!/usr/bin/env python3
"""Add a Cloudflare WARP egress to a node and route AI services through it.

Why: Gemini, Microsoft, and some AI services geo-block or degrade datacenter
IPs. Our nodes are hosting IPs, so those services return
"not available in your country". Routing ONLY the AI domains through a WARP
(Cloudflare WireGuard) outbound gives them a clean consumer egress IP, while
all other traffic keeps exiting via the node's own IP (no latency hit, no
single point of failure for the whole node — if WARP dies, only AI breaks).

The routing is scoped by SNI/hostname via geosite categories that ship in
geosite.dat (community-maintained, so the domain list isn't hand-kept):
  geosite:category-ai-!cn   all non-CN AI services (Gemini, OpenAI, Claude, ...)
  domain:labs.google        Google Labs family (Flow / Whisk / ImageFX / ...)
  geosite:google-gemini     Gemini specifically
  geosite:microsoft         Microsoft 365, Outlook, OneDrive, Teams, Xbox, etc.

Each node needs its OWN WARP registration — one key reused across IPs gets
flagged by Cloudflare. Xray's wireguard outbound is userspace (no host wg
interface, no kernel module needed, host routing untouched).

Usage:
    python3 setup_warp.py --host <ip> --password <root_pw> [--compose | --run]

  --compose : recreate the vpn container with `docker compose` (main, Sweden)
  --run     : recreate via `docker run` (USA — no compose plugin there)

The script registers WARP via wgcf, derives the `reserved` bytes from the
WARP client_id, injects the wireguard outbound + AI routing rule into the
live xray-config.json (preserved across restarts in the data volume), then
verifies the egress through a throwaway SOCKS inbound and removes it.
"""

from __future__ import annotations

import argparse
import base64
import json
import time

import paramiko

REMOTE_D = "/root/aegis/deploy/vps/data/vpn"
AI_DOMAINS = [
    "geosite:category-ai-!cn",
    "domain:labs.google",
    "geosite:google-gemini",
    "geosite:microsoft",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--password", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--compose", action="store_true")
    g.add_argument("--run", action="store_true")
    args = ap.parse_args()

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(
        args.host,
        username="root",
        password=args.password,
        timeout=25,
        look_for_keys=False,
        allow_agent=False,
    )

    def run(cmd: str, t: int = 150) -> str:
        _, o, e = c.exec_command(cmd, timeout=t)
        return (o.read() + e.read()).decode()

    def recreate() -> None:
        if args.compose:
            # Split topology: recreate the agent so its entrypoint rebuilds the
            # config with the WARP outbound from agent.env, then reload xray.
            run(
                "cd /root/aegis/deploy/vps && docker compose up -d --force-recreate agent "
                "&& docker compose restart xray",
                180,
            )
        else:
            # Legacy single-container path (non-compose nodes only).
            run("docker rm -f aegis-vpn 2>/dev/null; true", 60)
            run(
                "docker run -d --name aegis-vpn --restart unless-stopped --network host "
                "--log-opt max-size=5m --log-opt max-file=2 "
                "-v /root/aegis/deploy/vps/data/vpn:/data "
                "--env-file /root/aegis/deploy/vps/vpn.env aegis-vpn-live:latest",
                120,
            )

    # 1. register WARP
    url = run(
        "curl -fsSL https://api.github.com/repos/ViRb3/wgcf/releases/latest "
        "2>/dev/null | grep -oE 'https://[^\"]*linux_amd64' | head -1"
    ).strip()
    run(f"cd /root && curl -fsSL -o wgcf '{url}' && chmod +x wgcf")
    run("cd /root && yes | ./wgcf register")
    run("cd /root && ./wgcf generate")
    priv = run("grep PrivateKey /root/wgcf-profile.conf | cut -d' ' -f3").strip()
    addr = run("grep Address /root/wgcf-profile.conf | cut -d' ' -f3-").strip()
    peer = run("grep PublicKey /root/wgcf-profile.conf | cut -d' ' -f3").strip()
    tok = run("grep access_token /root/wgcf-account.toml | cut -d\\' -f2").strip()
    dev = run("grep device_id /root/wgcf-account.toml | cut -d\\' -f2").strip()
    reg = run(
        f"curl -fsS -m15 'https://api.cloudflareclient.com/v0a2158/reg/{dev}' "
        f"-H 'Authorization: Bearer {tok}' -H 'User-Agent: okhttp/3.12.1'"
    )
    reserved = list(base64.b64decode(json.loads(reg)["config"]["client_id"])[:3])
    addrs = [a.strip() for a in addr.split(",")]

    warp = {
        "protocol": "wireguard",
        "tag": "warp",
        "settings": {
            "secretKey": priv,
            "address": addrs,
            "peers": [{"publicKey": peer, "endpoint": "162.159.192.1:2408"}],
            "reserved": reserved,
            "mtu": 1280,
        },
    }
    print(f"WARP registered (reserved={reserved})")

    ts = time.strftime("%Y%m%d-%H%M%S")
    run(f"cp {REMOTE_D}/xray-config.json {REMOTE_D}/xray-config.json.bak-warp-{ts}")

    # 2. inject outbound + AI rule + throwaway socks for verification
    patch = (
        "import json;p='%s/xray-config.json';c=json.load(open(p));"
        "w=%s;"
        "c['outbounds']=[o for o in c['outbounds'] if o.get('tag')!='warp']+[w];"
        "ins=c.setdefault('inbounds',[]);"
        "ins.append({'listen':'127.0.0.1','port':10808,'protocol':'socks',"
        "'settings':{'udp':False},'tag':'warptest'}) "
        "if not any(i.get('tag')=='warptest' for i in ins) else None;"
        "r=c.setdefault('routing',{}).setdefault('rules',[]);"
        "r=[x for x in r if x.get('outboundTag')!='warp' and x.get('inboundTag')!=['warptest']];"
        "r.insert(0,{'type':'field','inboundTag':['warptest'],'outboundTag':'warp'});"
        "r.insert(0,{'type':'field','domain':%s,'outboundTag':'warp'});"
        "c['routing']['rules']=r;json.dump(c,open(p,'w'),indent=2);print('patched')"
        % (REMOTE_D, json.dumps(warp), json.dumps(AI_DOMAINS))
    )
    print(run(f"python3 -c {json.dumps(patch)}").strip())
    recreate()
    time.sleep(10)

    trace = run(
        "curl -fsS -m20 --socks5-hostname 127.0.0.1:10808 "
        "https://www.cloudflare.com/cdn-cgi/trace 2>&1 | grep -E 'warp=|loc=' | tr '\\n' ' '"
    ).strip()
    gem = run(
        "curl -s -m20 --socks5-hostname 127.0.0.1:10808 -o /dev/null "
        "-w '%{http_code}' https://gemini.google.com/ 2>&1"
    ).strip()
    print(f"egress: {trace} | gemini: {gem}")

    # 3. remove the throwaway socks
    cleanup = (
        "import json;p='%s/xray-config.json';c=json.load(open(p));"
        "c['inbounds']=[i for i in c['inbounds'] if i.get('tag')!='warptest'];"
        "c['routing']['rules']=[r for r in c['routing']['rules'] if r.get('inboundTag')!=['warptest']];"
        "json.dump(c,open(p,'w'),indent=2);print('cleaned')" % REMOTE_D
    )
    print(run(f"python3 -c {json.dumps(cleanup)}").strip())
    recreate()
    time.sleep(8)
    print("health:", run("curl -fsS -m6 http://127.0.0.1:8444/health; echo").strip())
    c.close()

    if "warp=on" in trace and gem == "200":
        print("OK")
        return 0
    print("WARNING: WARP egress check did not pass cleanly — verify manually")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
