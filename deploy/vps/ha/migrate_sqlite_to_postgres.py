#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine


def _json_value(value):
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def canonical_rows_digest(rows: list[dict]) -> str:
    normalized = [
        {key: _json_value(value) for key, value in sorted(row.items())}
        for row in rows
    ]
    normalized.sort(
        key=lambda row: json.dumps(
            row,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    payload = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


async def migrate(sqlite_path: Path, postgres_url: str) -> dict[str, dict]:
    from src.models import Base

    sqlite = create_async_engine(f"sqlite+aiosqlite:///{sqlite_path}")
    postgres = create_async_engine(postgres_url)
    report: dict[str, dict] = {}
    try:
        async with postgres.begin() as target:
            await target.run_sync(Base.metadata.create_all)
            for table in reversed(Base.metadata.sorted_tables):
                await target.execute(delete(table))

        for table in Base.metadata.sorted_tables:
            async with sqlite.connect() as source:
                rows = [dict(row) for row in (await source.execute(select(table))).mappings()]
            async with postgres.begin() as target:
                if rows:
                    await target.execute(insert(table), rows)
                sequence = await target.scalar(
                    text("SELECT pg_get_serial_sequence(:table, 'id')"),
                    {"table": table.name},
                )
                if sequence and any("id" in row for row in rows):
                    await target.execute(
                        text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                        {
                            "sequence": sequence,
                            "value": max(int(row["id"]) for row in rows if row.get("id") is not None),
                        },
                    )
            async with postgres.connect() as target:
                copied = [dict(row) for row in (await target.execute(select(table))).mappings()]
            source_digest = canonical_rows_digest(rows)
            target_digest = canonical_rows_digest(copied)
            if len(rows) != len(copied) or source_digest != target_digest:
                raise RuntimeError(f"migration verification failed for {table.name}")
            report[table.name] = {"rows": len(rows), "sha256": source_digest}
    finally:
        await sqlite.dispose()
        await postgres.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--postgres-url", required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = asyncio.run(migrate(args.sqlite, args.postgres_url))
    output = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
