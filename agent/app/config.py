from typing import Literal

from pydantic import ConfigDict, SecretStr, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    agent_token: str
    short_id: str
    public_key: str
    short_id_tcp: str | None = None
    public_key_tcp: str | None = None
    fingerprint: str = "firefox"
    tcp_fingerprint: str | None = "firefox"
    xray_port: int = 443
    xray_tcp_port: int | None = None
    xray_grpc_port: int | None = None
    grpc_service_name: str = "grpc"
    xray_network: str = "tcp"
    reality_dest: str = "gateway.icloud.com:443"
    reality_server_name: str = "gateway.icloud.com"
    host_ip: str  # Used for sub links
    fast_host_ip: str | None = None
    packet_encoding: str | None = "xudp"
    xhttp_path: str = "/"
    # "auto": server accepts packet-up + stream-up + stream-one; a client over
    # direct REALITY resolves auto to stream-one (single full-duplex stream).
    xhttp_mode: str = "auto"

    xray_config_path: str = "/etc/xray/config.json"
    # Local-only Xray gRPC API port (dokodemo-door inbound tagged "api").
    xray_api_port: int = 10085

    # Per-subscription simultaneous-IP limit (anti account-sharing).
    # 0 disables enforcement. Excess source IPs are blocked via xray api sib.
    conn_limit: int = 5
    conn_limit_interval: int = 60  # seconds between enforcement cycles

    # Local Hysteria2 process. Disabled by default: on a node without Hy2,
    # every Hy2 path is a no-op and the agent behaves exactly as before.
    hy2_enabled: bool = False
    hy2_stats_url: str = "http://127.0.0.1:9999"
    hy2_stats_secret: str | None = None
    hy2_certificate_path: str = "/data/hysteria/cert.pem"
    hy2_private_key_path: str = "/data/hysteria/key.pem"
    hy2_certificate_reload_marker: str = "/data/hysteria/.reload"
    hy2_certificate_check_seconds: int = 21_600

    # Outbound desired-state control plane. "off" preserves the legacy public
    # push API; "observe" verifies snapshots without mutation; "apply" makes
    # the downloaded complete state authoritative.
    control_mode: Literal["off", "observe", "apply"] = "off"
    control_urls: str = ""
    control_token: SecretStr | None = None
    control_token_file: str = "/data/control/token"
    control_client_cert: str = "/data/control/client.crt"
    control_client_key: str = "/data/control/client.key"
    control_ca_cert: str = "/data/control/ca.crt"
    control_timeout_seconds: int = 40
    control_max_page_bytes: int = 1_048_576
    control_max_snapshot_bytes: int = 64 * 1_048_576

    @field_validator(
        "xray_tcp_port", "xray_grpc_port", "fast_host_ip",
        "short_id_tcp", "public_key_tcp", "hy2_stats_secret", "control_token",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v):
        # A fresh node's docker-compose passes UNSET optional vars as "" (the
        # ${VAR:-} default), and an empty string can't coerce to int|None — it
        # crash-loops the agent (xray_grpc_port='' after gRPC was dropped). Treat
        # a blank string as unset so the field falls back to its None default.
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @property
    def control_url_list(self) -> list[str]:
        return [
            url.strip().rstrip("/")
            for url in self.control_urls.split(",")
            if url.strip()
        ]

    model_config = ConfigDict(
        env_file="/data/agent.env",
        extra="ignore",
    )


settings = Settings()  # type: ignore
