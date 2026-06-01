from pydantic import ConfigDict
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
    xray_network: str = "tcp"
    reality_dest: str = "gateway.icloud.com:443"
    reality_server_name: str = "gateway.icloud.com"
    host_ip: str  # Used for sub links
    fast_host_ip: str | None = None
    packet_encoding: str | None = "xudp"
    xhttp_path: str = "/"
    xhttp_mode: str = "packet-up"

    xray_config_path: str = "/etc/xray/config.json"
    # Local-only Xray gRPC API port (dokodemo-door inbound tagged "api").
    xray_api_port: int = 10085

    # Per-subscription simultaneous-IP limit (anti account-sharing).
    # 0 disables enforcement. Excess source IPs are blocked via xray api sib.
    conn_limit: int = 5
    conn_limit_interval: int = 60  # seconds between enforcement cycles

    model_config = ConfigDict(
        env_file="/data/agent.env",
        extra="ignore",
    )


settings = Settings()  # type: ignore
