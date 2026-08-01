import json
import os
import tempfile
import time
from dataclasses import dataclass

from . import cascade, connlimit, hysteria
from .config import settings
from .control_models import (
    AppliedState,
    DesiredCascadeRoute,
    DesiredCascadeService,
    DesiredClient,
    DesiredConnLimit,
    DesiredSnapshot,
)
from .xray import (
    build_client_record,
    config_lock,
    get_xray_config,
    list_vless_inbounds,
    reload_xray,
    save_xray_config,
    wait_for_xray_ready,
    xray_api_add,
    xray_api_remove,
)

_APPLIED_STATE_PATH = "/data/node-control/applied-state.json"
_PENDING_REVOCATIONS_PATH = "/data/node-control/pending-revocations.json"


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    success: bool
    changed: bool
    observed: bool
    added: int
    removed: int


def load_applied_state() -> AppliedState:
    try:
        with open(_APPLIED_STATE_PATH) as file:
            return AppliedState.model_validate_json(file.read())
    except (OSError, ValueError):
        return AppliedState(generation=0, digest=None)


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(
        prefix=".control-state-",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(
                payload,
                file,
                sort_keys=True,
                separators=(",", ":"),
            )
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.remove(temporary_path)
        except FileNotFoundError:
            pass


def save_applied_state(state: AppliedState) -> None:
    _atomic_write_json(
        _APPLIED_STATE_PATH,
        state.model_dump(mode="json"),
    )


def load_pending_revocations() -> set[str]:
    try:
        with open(_PENDING_REVOCATIONS_PATH) as file:
            payload = json.load(file)
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, TypeError) as exc:
        raise ReconcileError("invalid pending revocations journal") from exc
    emails = payload.get("emails") if isinstance(payload, dict) else None
    if not isinstance(emails, list) or not all(isinstance(email, str) and email for email in emails):
        raise ReconcileError("invalid pending revocations journal")
    return set(emails)


def save_pending_revocations(values: set[str]) -> None:
    _atomic_write_json(
        _PENDING_REVOCATIONS_PATH,
        {"emails": sorted(values)},
    )


def _configured_emails(config: dict) -> set[str]:
    return {
        str(email)
        for inbound in list_vless_inbounds(config)
        for record in inbound.get("settings", {}).get("clients", [])
        if (email := record.get("email"))
    }


async def retry_pending_revocations(config: dict | None = None) -> bool:
    """Retry durable Hysteria kicks that are safe under current auth state.

    An email still present in the current Xray config is authorized and must not
    be cleared from the journal by a kick: it could immediately reconnect. Such
    entries remain pending until an authoritative reconcile removes them or a
    newer desired state explicitly re-authorizes them.
    """
    pending = load_pending_revocations()
    if not pending:
        return True
    current = config if config is not None else await get_xray_config()
    hysteria.refresh_from_config(current)
    safe_to_kick = pending - _configured_emails(current)
    if safe_to_kick and not await hysteria.kick(sorted(safe_to_kick)):
        return False
    remaining = pending - safe_to_kick
    save_pending_revocations(remaining)
    return not remaining


def _record_projection(record: dict) -> dict:
    projected = {
        "id": record.get("id"),
        "email": record.get("email"),
    }
    if record.get("flow"):
        projected["flow"] = record["flow"]
    return projected


def _desired_parts(
    snapshot: DesiredSnapshot,
    *,
    now_ms: int,
) -> tuple[list[DesiredClient], dict[int, int], list[DesiredCascadeRoute]]:
    clients: list[DesiredClient] = []
    routes: list[DesiredCascadeRoute] = []
    for item in snapshot.items:
        if isinstance(item, DesiredClient) and item.expire_ms > now_ms:
            clients.append(item)
        elif isinstance(item, DesiredCascadeService):
            clients.append(
                DesiredClient(
                    kind="client",
                    uuid=item.uuid,
                    email=item.email,
                    expire_ms=4_102_444_800_000,
                )
            )
        elif isinstance(item, DesiredCascadeRoute):
            routes.append(item)
    overrides = {item.user_id: item.limit for item in snapshot.items if isinstance(item, DesiredConnLimit)}
    return clients, overrides, routes


async def reconcile_snapshot(
    snapshot: DesiredSnapshot,
    *,
    observe: bool,
    now_ms: int | None = None,
) -> ReconcileResult:
    effective_now_ms = now_ms if now_ms is not None else time.time_ns() // 1_000_000
    desired_clients, desired_overrides, desired_routes = _desired_parts(
        snapshot,
        now_ms=effective_now_ms,
    )

    async with config_lock:
        config = await get_xray_config()
        desired_emails = {client.email for client in desired_clients}
        pending_revocations = load_pending_revocations()
        effective_pending = pending_revocations - desired_emails
        cascade_changed = cascade.apply_cascade_routes(config, desired_routes)
        additions: list[tuple[dict, dict]] = []
        removals: list[tuple[str, str]] = []
        removed_emails: set[str] = set()
        config_changed = cascade_changed

        for inbound in list_vless_inbounds(config):
            existing = list(inbound.setdefault("settings", {}).setdefault("clients", []))
            existing_by_uuid = {record.get("id"): record for record in existing if record.get("id")}
            desired_records = [build_client_record(item.uuid, item.email, inbound) for item in desired_clients]
            desired_by_uuid = {record["id"]: record for record in desired_records}

            for uuid, record in existing_by_uuid.items():
                desired_record = desired_by_uuid.get(uuid)
                if desired_record is not None and _record_projection(record) == desired_record:
                    continue
                email = record.get("email")
                tag = inbound.get("tag")
                if tag and email:
                    removals.append((tag, email))
                    removed_emails.add(email)

            for uuid, desired_record in desired_by_uuid.items():
                existing_record = existing_by_uuid.get(uuid)
                if existing_record is not None and _record_projection(existing_record) == desired_record:
                    continue
                additions.append((inbound, desired_record))

            if existing != desired_records:
                inbound["settings"]["clients"] = desired_records
                config_changed = True

        override_changed = dict(connlimit._overrides) != desired_overrides
        previous = load_applied_state()
        state_changed = (
            previous.generation != snapshot.generation
            or previous.digest != snapshot.digest
            or previous.items != snapshot.items
        )
        changed = (
            config_changed
            or override_changed
            or state_changed
            or bool(effective_pending)
            or effective_pending != pending_revocations
        )
        result = ReconcileResult(
            success=True,
            changed=changed,
            observed=observe,
            added=len(additions),
            removed=len(removals),
        )
        if observe or not changed:
            return result

        newly_revoked = removed_emails - desired_emails
        pending_revocations = effective_pending | newly_revoked
        if pending_revocations != load_pending_revocations():
            save_pending_revocations(pending_revocations)

        if config_changed:
            await save_xray_config(config)

        live_delta_size = len(removals) + len(additions)
        use_live_api = not cascade_changed and live_delta_size <= max(0, settings.xray_live_delta_limit)
        api_ok = use_live_api
        if use_live_api:
            for tag, email in removals:
                if not await xray_api_remove(tag, email):
                    api_ok = False
            for inbound, record in additions:
                if not await xray_api_add(inbound, record):
                    api_ok = False
        if not api_ok:
            reload_xray()
            if not await wait_for_xray_ready():
                raise ReconcileError("live Xray reconciliation failed")

        if pending_revocations and not await retry_pending_revocations(config):
            raise ReconcileError("Hysteria session removal failed")

        try:
            if override_changed:
                connlimit.replace_overrides(desired_overrides)
            save_applied_state(
                AppliedState(
                    schema_version=snapshot.schema_version,
                    generation=snapshot.generation,
                    digest=snapshot.digest,
                    items=snapshot.items,
                )
            )
        except OSError as exc:
            raise ReconcileError("failed to persist applied control state") from exc
        return result
