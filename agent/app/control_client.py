import hashlib
import json
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from pydantic import ValidationError

from .config import settings
from .control_models import (
    AppliedState,
    DesiredCascadeService,
    DesiredClient,
    DesiredSnapshot,
    SnapshotManifest,
    SnapshotPage,
)


class ControlProtocolError(RuntimeError):
    pass


class ControlRequestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RawControlResponse:
    status: int
    body: bytes


Requester = Callable[..., Awaitable[RawControlResponse]]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def build_ssl_context(
    *,
    ca_path: str,
    cert_path: str,
    key_path: str,
) -> ssl.SSLContext:
    # Keep the operating system's public trust store for a normal ACME-issued
    # control hostname, then add the operator CA as an optional extra trust
    # anchor. Passing ``cafile`` directly to create_default_context replaces the
    # defaults on CPython and rejects Let's Encrypt/ZeroSSL certificates.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=ca_path)
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context


class ControlClient:
    def __init__(
        self,
        *,
        urls: list[str],
        token: str,
        ssl_context: ssl.SSLContext | object,
        requester: Requester | None = None,
        timeout_seconds: float = 40,
        max_page_bytes: int = 1_048_576,
        max_snapshot_bytes: int = 64 * 1_048_576,
        agent_version: str = "0.1.0",
        capabilities: list[str] | None = None,
    ):
        normalized_urls = [url.rstrip("/") for url in urls if url.strip()]
        if not normalized_urls:
            raise ValueError("at least one control URL is required")
        if any(not url.startswith("https://") for url in normalized_urls):
            raise ValueError("control URLs must use https")
        if max_page_bytes < 1 or max_snapshot_bytes < max_page_bytes:
            raise ValueError("invalid control response byte limits")

        self.urls = normalized_urls
        self.headers = {"Authorization": f"Bearer {token}"}
        self.ssl_context = ssl_context
        self.timeout_seconds = timeout_seconds
        self.max_page_bytes = max_page_bytes
        self.max_snapshot_bytes = max_snapshot_bytes
        self.agent_version = agent_version
        self.capabilities = capabilities or []
        self._session: aiohttp.ClientSession | None = None
        self._requester = requester or self._aiohttp_request
        self._preferred_url: str | None = None

    @classmethod
    def from_settings(cls) -> "ControlClient":
        token = (
            settings.control_token.get_secret_value()
            if settings.control_token is not None
            else Path(settings.control_token_file).read_text().strip()
        )
        if not token:
            raise ValueError("node control token is empty")
        return cls(
            urls=settings.control_url_list,
            token=token,
            ssl_context=build_ssl_context(
                ca_path=settings.control_ca_cert,
                cert_path=settings.control_client_cert,
                key_path=settings.control_client_key,
            ),
            timeout_seconds=settings.control_timeout_seconds,
            max_page_bytes=settings.control_max_page_bytes,
            max_snapshot_bytes=settings.control_max_snapshot_bytes,
            capabilities=["cascade-v2"],
        )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _aiohttp_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        ssl: ssl.SSLContext,
        json: dict | None,
        timeout: float,
        max_bytes: int,
    ) -> RawControlResponse:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        request_timeout = aiohttp.ClientTimeout(total=timeout)
        async with self._session.request(
            method,
            url,
            headers=headers,
            ssl=ssl,
            json=json,
            timeout=request_timeout,
        ) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise ControlProtocolError("control response exceeds byte limit")
            return RawControlResponse(status=response.status, body=bytes(body))

    def _ordered_urls(self) -> list[str]:
        if self._preferred_url not in self.urls:
            return list(self.urls)
        return [
            self._preferred_url,
            *(url for url in self.urls if url != self._preferred_url),
        ]

    async def _request_with_failover(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None,
        max_bytes: int,
    ) -> RawControlResponse:
        last_error: Exception | None = None
        for base_url in self._ordered_urls():
            try:
                response = await self._requester(
                    method,
                    f"{base_url}{path}",
                    headers=self.headers,
                    ssl=self.ssl_context,
                    json=json_body,
                    timeout=self.timeout_seconds,
                    max_bytes=max_bytes,
                )
                if len(response.body) > max_bytes:
                    raise ControlProtocolError("control response exceeds byte limit")
                if response.status >= 500:
                    last_error = ControlRequestError(
                        f"control endpoint returned {response.status}"
                    )
                    continue
                self._preferred_url = base_url
                return response
            except ControlProtocolError:
                raise
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                last_error = exc
        raise ControlRequestError("all control endpoints are unavailable") from last_error

    @staticmethod
    def _parse_json(response: RawControlResponse) -> Any:
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ControlProtocolError("control response is not valid JSON") from exc

    @staticmethod
    def _validate_schema_version(payload: Any) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in {1, 2}
        ):
            raise ControlProtocolError("unsupported schema version")

    async def sync(self, applied: AppliedState) -> DesiredSnapshot | None:
        response = await self._request_with_failover(
            "POST",
            "/api/node/v1/sync",
            json_body={
                "applied_generation": applied.generation,
                "applied_digest": applied.digest,
                "agent_version": self.agent_version,
                "capabilities": self.capabilities,
            },
            max_bytes=self.max_page_bytes,
        )
        if response.status == 204:
            return None
        if response.status != 200:
            raise ControlRequestError(
                f"control sync rejected with status {response.status}"
            )

        manifest_payload = self._parse_json(response)
        self._validate_schema_version(manifest_payload)
        try:
            manifest = SnapshotManifest.model_validate(manifest_payload)
        except ValidationError as exc:
            raise ControlProtocolError("invalid snapshot manifest") from exc

        items: list[dict] = []
        total_bytes = 0
        seen_client_uuids: set[str] = set()
        for page_index in range(manifest.page_count):
            page_response = await self._request_with_failover(
                "GET",
                (
                    f"/api/node/v1/snapshots/{manifest.generation}"
                    f"/pages/{page_index}"
                ),
                json_body=None,
                max_bytes=self.max_page_bytes,
            )
            if page_response.status != 200:
                raise ControlRequestError(
                    f"snapshot page rejected with status {page_response.status}"
                )
            page_payload = self._parse_json(page_response)
            self._validate_schema_version(page_payload)
            try:
                page = SnapshotPage.model_validate(page_payload)
            except ValidationError as exc:
                raise ControlProtocolError("invalid snapshot page") from exc
            if (
                page.schema_version != manifest.schema_version
                or page.generation != manifest.generation
                or page.page_index != page_index
            ):
                raise ControlProtocolError("snapshot page identity mismatch")

            page_items = [item.model_dump(mode="json") for item in page.items]
            actual_page_digest = hashlib.sha256(
                _canonical_json(page_items)
            ).hexdigest()
            if actual_page_digest != page.page_digest:
                raise ControlProtocolError("snapshot page digest mismatch")
            for item in page.items:
                if isinstance(item, (DesiredClient, DesiredCascadeService)):
                    if item.uuid in seen_client_uuids:
                        raise ControlProtocolError("duplicate client UUID")
                    seen_client_uuids.add(item.uuid)
            items.extend(page_items)
            total_bytes += len(_canonical_json(page_items))
            if total_bytes > self.max_snapshot_bytes:
                raise ControlProtocolError("snapshot exceeds byte limit")

        if len(items) != manifest.item_count:
            raise ControlProtocolError("snapshot item count mismatch")
        actual_digest = hashlib.sha256(_canonical_json(items)).hexdigest()
        if actual_digest != manifest.digest:
            raise ControlProtocolError("snapshot digest mismatch")
        try:
            return DesiredSnapshot(
                schema_version=manifest.schema_version,
                generation=manifest.generation,
                digest=manifest.digest,
                items=items,
            )
        except ValidationError as exc:
            raise ControlProtocolError("invalid desired snapshot") from exc

    async def ack(
        self,
        *,
        generation: int,
        digest: str,
        success: bool,
        error: str | None,
    ) -> None:
        response = await self._request_with_failover(
            "POST",
            "/api/node/v1/ack",
            json_body={
                "generation": generation,
                "digest": digest,
                "success": success,
                "error": error,
            },
            max_bytes=64 * 1024,
        )
        if response.status != 200:
            raise ControlRequestError(
                f"control acknowledgement rejected with status {response.status}"
            )

    async def send_telemetry(
        self,
        *,
        sequence: int,
        payload: dict,
    ) -> None:
        response = await self._request_with_failover(
            "POST",
            "/api/node/v1/telemetry",
            json_body={
                "sequence": sequence,
                "payload": payload,
            },
            max_bytes=64 * 1024,
        )
        if response.status != 200:
            raise ControlRequestError(
                f"control telemetry rejected with status {response.status}"
            )

    async def get_hy2_certificate(self) -> dict[str, str] | None:
        response = await self._request_with_failover(
            "GET",
            "/api/node/v1/hy2-certificate",
            json_body=None,
            max_bytes=512 * 1024,
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise ControlRequestError(
                f"certificate endpoint rejected with status {response.status}"
            )
        payload = self._parse_json(response)
        required = {"certificate", "private_key", "hostname", "fingerprint"}
        if (
            not isinstance(payload, dict)
            or not required.issubset(payload)
            or not all(isinstance(payload[key], str) for key in required)
        ):
            raise ControlProtocolError("invalid certificate bundle")
        return payload
