from __future__ import annotations

from copy import deepcopy

from .control_models import DesiredCascadeRoute

_TAG_PREFIX = "cascade-"


class CascadeConfigError(ValueError):
    pass


def _outbound(route: DesiredCascadeRoute, index: int) -> dict:
    exit_node = route.exits[index]
    tag = f"{_TAG_PREFIX}{route.route_id}-exit-{exit_node.position}"
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": exit_node.host,
                    "port": exit_node.port,
                    "users": [
                        {
                            "id": exit_node.uuid,
                            "encryption": "none",
                        }
                    ],
                }
            ]
        },
        "streamSettings": {
            "network": "xhttp",
            "security": "reality",
            "realitySettings": {
                "publicKey": exit_node.public_key,
                "shortId": exit_node.short_id,
                "serverName": exit_node.server_name,
                "fingerprint": "chrome",
                "show": False,
            },
            "xhttpSettings": {
                "path": exit_node.xhttp_path or "/",
                "mode": "packet-up",
            },
        },
    }


def _strip_managed(config: dict) -> None:
    routing = config.get("routing") or {}
    had_managed = any(
        str(outbound.get("tag", "")).startswith(_TAG_PREFIX)
        for outbound in config.get("outbounds", [])
    ) or any(
        str(balancer.get("tag", "")).startswith(_TAG_PREFIX)
        for balancer in routing.get("balancers", [])
    )
    if "outbounds" in config:
        config["outbounds"] = [
            outbound
            for outbound in config["outbounds"]
            if not str(outbound.get("tag", "")).startswith(_TAG_PREFIX)
        ]
    if "routing" in config:
        routing["rules"] = [
            rule
            for rule in routing.get("rules", [])
            if not str(rule.get("balancerTag", "")).startswith(_TAG_PREFIX)
        ]
        balancers = [
            balancer
            for balancer in routing.get("balancers", [])
            if not str(balancer.get("tag", "")).startswith(_TAG_PREFIX)
        ]
        if balancers:
            routing["balancers"] = balancers
        else:
            routing.pop("balancers", None)
    if had_managed:
        config.pop("observatory", None)


def apply_cascade_routes(
    config: dict,
    routes: list[DesiredCascadeRoute],
) -> bool:
    """Apply the exact cascade state.

    Client-facing entry inbounds are routed only to the balancer. There is no
    freedom/direct fallback, so loss of every foreign exit fails closed.
    """
    before = deepcopy(config)
    _strip_managed(config)
    if not routes:
        return config != before

    claimed_inbounds: set[str] = set()
    all_subjects: list[str] = []
    managed_rules: list[dict] = []
    managed_balancers: list[dict] = []
    for route in sorted(routes, key=lambda item: item.route_id):
        overlap = claimed_inbounds.intersection(route.inbound_tags)
        if overlap:
            raise CascadeConfigError(
                f"cascade inbound is claimed by multiple routes: {sorted(overlap)}"
            )
        claimed_inbounds.update(route.inbound_tags)
        strategy = str(route.health_policy.get("strategy") or "leastPing")
        if strategy != "leastPing":
            raise CascadeConfigError(
                f"unsupported cascade health strategy: {strategy}"
            )
        if any(not exit_node.xhttp_path.startswith("/") for exit_node in route.exits):
            raise CascadeConfigError("cascade XHTTP path must start with '/'")
        outbounds = [_outbound(route, index) for index in range(len(route.exits))]
        tags = [outbound["tag"] for outbound in outbounds]
        config.setdefault("outbounds", []).extend(outbounds)
        all_subjects.extend(tags)
        balancer_tag = f"{_TAG_PREFIX}{route.route_id}-balancer"
        managed_balancers.append(
            {
                "tag": balancer_tag,
                "selector": tags,
                "strategy": {"type": strategy},
            }
        )
        managed_rules.append(
            {
                "type": "field",
                "inboundTag": list(route.inbound_tags),
                "balancerTag": balancer_tag,
            }
        )

    routing = config.setdefault("routing", {})
    # Route rules must precede the final catch-all direct rule.
    routing["rules"] = managed_rules + routing.get("rules", [])
    routing["balancers"] = managed_balancers
    config["observatory"] = {
        "subjectSelector": all_subjects,
        "probeUrl": str(
            routes[0].health_policy.get("probe_url")
            or "https://www.gstatic.com/generate_204"
        ),
        "probeInterval": str(
            routes[0].health_policy.get("probe_interval") or "10s"
        ),
        "enableConcurrency": True,
    }
    return config != before
