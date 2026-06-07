from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: SecretStr
    admin_ids: list[int]
    bot_public_url: str | None = None
    premium_bot_url: str = "https://t.me/PremiumBot"
    subscription_public_base_url: str | None = None

    bot_domain: str | None = None
    public_base_url: str | None = None
    telegram_mode: str = "webhook"

    database_url: str | None = None
    db_host: str = "db"
    db_port: int = 5432
    db_user: str = "postgres"
    db_pass: SecretStr | None = None
    db_name: str = "aegis"
    db_echo: bool = False
    auto_init_db: bool = False
    sqlite_path: str = "/data/aegis.db"

    webhook_path: str = "/webhook"
    webapp_host: str = "0.0.0.0"
    webapp_port: int = 8080

    site_title: str = "Aegis VPN"
    site_description: str = "Simple VPN landing page and subscription endpoint"
    subscription_title: str = "AegisVPN"
    subscription_update_interval_hours: int = 1

    bootstrap_plans_json: str = ""
    bootstrap_server_name: str = "Main"
    bootstrap_server_flag: str = "VPN"
    bootstrap_server_agent_env: str = "/vpn-data/agent.env"
    bootstrap_server_agent_url: str = "http://127.0.0.1:8444"
    bootstrap_server_wait_seconds: int = 30
    bootstrap_server_subscription_group: str = "safe"

    log_level: str = "INFO"

    # Daily DB backup: a compressed dump is sent to each admin in Telegram.
    backup_enabled: bool = True
    backup_hour: int = 4  # UTC hour for the daily backup
    backup_keep: int = 7  # local rotated copies kept under /data/backups

    # Offline GeoIP (DB-IP City Lite) for the "added from" location on devices.
    # Downloaded into the persistent /data volume on first boot, refreshed monthly.
    geoip_enabled: bool = True
    geoip_db_path: str = "/data/geoip/dbip-city-lite.mmdb"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url

        if self.db_pass is None:
            return f"sqlite+aiosqlite:///{self.sqlite_path}"

        return f"postgresql+asyncpg://{self.db_user}:{self.db_pass.get_secret_value()}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")

        if self.bot_domain:
            domain = self.bot_domain.rstrip("/")
            if domain.startswith(("http://", "https://")):
                return domain
            return f"https://{domain}"

        return f"http://127.0.0.1:{self.webapp_port}"

    @property
    def subscription_base_url(self) -> str:
        if self.subscription_public_base_url:
            return self.subscription_public_base_url.rstrip("/")
        return self.base_url

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()  # type: ignore
