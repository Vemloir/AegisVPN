#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

SERVER_PORT="${SERVER_PORT:-51820}"
SERVER_ADDRESS="${SERVER_ADDRESS:-10.244.0.1/16}"
SERVER_NETWORK="${SERVER_NETWORK:-10.244.0.0/16}"
Jc="${Jc:-5}"
Jmin="${Jmin:-64}"
Jmax="${Jmax:-256}"
S1="${S1:-32}"
S2="${S2:-64}"
H1="${H1:-12452345}"
H2="${H2:-24563456}"
H3="${H3:-35674567}"
H4="${H4:-46785678}"

apt-get update
apt-get install -y software-properties-common python3 python3-pip openssh-server
if ! grep -Rqs "ppa.launchpadcontent.net/amnezia/ppa" /etc/apt/sources.list /etc/apt/sources.list.d; then
  add-apt-repository -y ppa:amnezia/ppa
  apt-get update
fi
apt-get install -y amneziawg amneziawg-tools

mkdir -p /etc/amneziawg /root/aegis/deploy/vps

if [ ! -f /etc/amneziawg/server.key ] || [ ! -f /etc/amneziawg/server.pub ]; then
  umask 077
  awg genkey | tee /etc/amneziawg/server.key | awg pubkey > /etc/amneziawg/server.pub
fi

UPLINK_IFACE="$(ip route show default | awk '{print $5; exit}')"

cat > /etc/amneziawg/aegis_awg.env <<EOF
SERVER_PRIVATE_KEY=$(cat /etc/amneziawg/server.key)
SERVER_PUBLIC_KEY=$(cat /etc/amneziawg/server.pub)
SERVER_ADDRESS=${SERVER_ADDRESS}
SERVER_NETWORK=${SERVER_NETWORK}
SERVER_PORT=${SERVER_PORT}
UPLINK_IFACE=${UPLINK_IFACE}
Jc=${Jc}
Jmin=${Jmin}
Jmax=${Jmax}
S1=${S1}
S2=${S2}
H1=${H1}
H2=${H2}
H3=${H3}
H4=${H4}
EOF

cat > /etc/sysctl.d/99-aegis-amnezia.conf <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sysctl --system >/dev/null

touch /etc/amneziawg/peers.json
chmod 600 /etc/amneziawg/server.key /etc/amneziawg/aegis_awg.env /etc/amneziawg/peers.json
ufw allow "${SERVER_PORT}/udp" >/dev/null 2>&1 || true

if [ -f /root/aegis/deploy/vps/apply_amnezia_peers.py ]; then
  python3 /root/aegis/deploy/vps/apply_amnezia_peers.py || true
fi

echo "AMNEZIA_SERVER_PUBLIC_KEY=$(cat /etc/amneziawg/server.pub)"
echo "AMNEZIA_SERVER_PORT=${SERVER_PORT}"
