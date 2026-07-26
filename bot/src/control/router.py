import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import update

from src.control.auth import authenticate_node
from src.control.certificates import load_hy2_certificate_bundle
from src.control.schemas import (
    NodeAckRequest,
    NodeControlResult,
    NodeSyncRequest,
    NodeTelemetryRequest,
    SnapshotManifest,
    SnapshotPage,
)
from src.control.state import canonical_json, publish_snapshot
from src.core.config import settings
from src.core.database import async_session_maker
from src.models import (
    NodeSnapshot,
    NodeSnapshotPage,
    NodeTelemetry,
    Server,
    SubscriptionServer,
)
from src.services.cascade_service import record_cascade_ack

router = APIRouter(prefix="/api/node/v1", tags=["node-control"])


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@router.get("/hy2-certificate")
async def get_hy2_certificate(
    response: Response,
    node: Server = Depends(authenticate_node),
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if not node.hy2_capable:
        raise HTTPException(
            status_code=404,
            detail="Hy2 is disabled",
            headers={"Cache-Control": "no-store"},
        )
    try:
        bundle = load_hy2_certificate_bundle(settings.node_hy2_certificate_dir)
    except (OSError, ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=503,
            detail="Hy2 certificate is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None
    if bundle.hostname != (node.hy2_sni or "").strip().lower():
        raise HTTPException(
            status_code=409,
            detail="Hy2 hostname mismatch",
            headers={"Cache-Control": "no-store"},
        )
    return {
        "certificate": bundle.certificate,
        "private_key": bundle.private_key,
        "hostname": bundle.hostname,
        "fingerprint": bundle.fingerprint,
        "not_after": bundle.not_after.isoformat(),
    }


@router.post("/sync", response_model=SnapshotManifest)
async def sync_node(
    request: NodeSyncRequest,
    node: Server = Depends(authenticate_node),
) -> SnapshotManifest | Response:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, settings.node_control_long_poll_seconds)

    while True:
        async with async_session_maker() as session:
            current = await session.get(Server, node.id)
            if (
                current is None
                or current.control_mode not in {"observe", "pull"}
            ):
                raise HTTPException(status_code=401, detail="Inactive node")
            current.control_last_seen_at = _utcnow()
            current.control_agent_version = request.agent_version
            current.control_capabilities = {"features": request.capabilities}
            snapshot = await publish_snapshot(
                session,
                current.id,
                page_size=settings.node_control_page_size,
            )
            await session.commit()

        if (
            snapshot.generation > request.applied_generation
            or request.applied_digest != snapshot.digest
        ):
            return SnapshotManifest(
                schema_version=snapshot.schema_version,
                generation=snapshot.generation,
                digest=snapshot.digest,
                item_count=snapshot.item_count,
                page_count=snapshot.page_count,
                page_size=snapshot.page_size,
            )

        remaining = deadline - loop.time()
        if remaining <= 0:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        await asyncio.sleep(
            min(settings.node_control_poll_interval_seconds, remaining)
        )


@router.get(
    "/snapshots/{generation}/pages/{page_index}",
    response_model=SnapshotPage,
)
async def get_snapshot_page(
    generation: int,
    page_index: int,
    node: Server = Depends(authenticate_node),
) -> SnapshotPage:
    async with async_session_maker() as session:
        page = await session.get(
            NodeSnapshotPage,
            {
                "server_id": node.id,
                "generation": generation,
                "page_index": page_index,
            },
        )
    if page is None:
        raise HTTPException(status_code=404, detail="Snapshot page not found")
    return SnapshotPage(
        schema_version=page.schema_version,
        generation=page.generation,
        page_index=page.page_index,
        page_digest=page.page_digest,
        items=page.items,
    )


@router.post("/ack", response_model=NodeControlResult)
async def acknowledge_snapshot(
    request: NodeAckRequest,
    node: Server = Depends(authenticate_node),
) -> NodeControlResult:
    async with async_session_maker() as session:
        snapshot = await session.get(
            NodeSnapshot,
            {"server_id": node.id, "generation": request.generation},
        )
        current = await session.get(Server, node.id)
        if snapshot is None or current is None:
            raise HTTPException(status_code=404, detail="Snapshot not found")
        if not hmac_digest_matches(snapshot.digest, request.digest):
            raise HTTPException(status_code=409, detail="Snapshot digest mismatch")

        current.control_last_seen_at = _utcnow()
        if not request.success:
            current.control_last_error = request.error or "reconciliation failed"
            await session.commit()
            return NodeControlResult(status="error-recorded")

        if request.generation < int(current.applied_generation or 0):
            await session.commit()
            return NodeControlResult(status="duplicate")
        if request.generation == int(current.applied_generation or 0):
            await session.commit()
            return NodeControlResult(status="duplicate")

        current.applied_generation = request.generation
        current.applied_digest = request.digest
        current.control_last_reconciled_at = _utcnow()
        current.control_last_error = None
        await record_cascade_ack(
            session,
            server_id=node.id,
            generation=request.generation,
        )
        await session.execute(
            update(SubscriptionServer)
            .where(SubscriptionServer.server_id == node.id)
            .values(is_synced=True)
        )
        await session.commit()
    return NodeControlResult(status="ok")


def hmac_digest_matches(expected: str, actual: str) -> bool:
    import hmac

    return hmac.compare_digest(expected, actual)


@router.post("/telemetry", response_model=NodeControlResult)
async def receive_telemetry(
    request: NodeTelemetryRequest,
    node: Server = Depends(authenticate_node),
) -> NodeControlResult:
    if len(canonical_json(request.payload)) > settings.node_control_max_telemetry_bytes:
        raise HTTPException(status_code=413, detail="Telemetry payload too large")

    async with async_session_maker() as session:
        current = await session.get(Server, node.id)
        if current is None:
            raise HTTPException(status_code=401, detail="Inactive node")
        current.control_last_seen_at = _utcnow()
        telemetry = await session.get(NodeTelemetry, node.id)
        if telemetry is not None and request.sequence <= telemetry.sequence:
            await session.commit()
            return NodeControlResult(status="duplicate")
        if telemetry is None:
            telemetry = NodeTelemetry(
                server_id=node.id,
                sequence=request.sequence,
                payload=request.payload,
                received_at=_utcnow(),
            )
            session.add(telemetry)
        else:
            telemetry.sequence = request.sequence
            telemetry.payload = request.payload
            telemetry.received_at = _utcnow()
        await session.commit()
    return NodeControlResult(status="ok")
