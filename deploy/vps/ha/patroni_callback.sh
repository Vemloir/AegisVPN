#!/bin/sh
set -eu

action=${1:-}
role=${2:-}

# HA can be rehearsed before DNS automation is enabled.
[ -n "${CLOUDFLARE_API_TOKEN:-}" ] || exit 0

case "$action" in
  on_start|on_role_change|on_restart)
    ;;
  *)
    exit 0
    ;;
esac

case "$role" in
  primary|master|leader)
    ;;
  *)
    exit 0
    ;;
esac

exec python3 /opt/aegis-ha/cloudflare_failover.py --role "$role"
