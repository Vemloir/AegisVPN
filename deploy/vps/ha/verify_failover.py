#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json

import asyncpg


def validate_cluster_state(states: list[dict]) -> dict:
    writable = [state for state in states if state.get("writable") is True]
    if len(writable) != 1:
        raise ValueError(f"expected exactly one writable node, got {len(writable)}")
    leader_timeline = int(writable[0].get("timeline") or 0)
    if any(int(state.get("timeline") or 0) > leader_timeline for state in states):
        raise ValueError("a replica reports a timeline ahead of the writable node")
    return writable[0]


async def inspect_node(name: str, url: str) -> dict:
    connection = await asyncpg.connect(url)
    try:
        row = await connection.fetchrow(
            "SELECT NOT pg_is_in_recovery() AS writable, "
            "(pg_control_checkpoint()).timeline_id AS timeline"
        )
        return {"name": name, "writable": row["writable"], "timeline": row["timeline"]}
    finally:
        await connection.close()


async def run(nodes: list[tuple[str, str]]) -> list[dict]:
    states = await asyncio.gather(*(inspect_node(name, url) for name, url in nodes))
    validate_cluster_state(states)
    return states


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", action="append", required=True, metavar="NAME=URL")
    args = parser.parse_args()
    nodes = [tuple(value.split("=", 1)) for value in args.node]
    print(json.dumps(asyncio.run(run(nodes)), indent=2))


if __name__ == "__main__":
    main()
