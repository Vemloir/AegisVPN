from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Token of the support bot (@AegisVPNsupportBot). NEVER commit it — read from
    # the environment / a gitignored support.env, like the main bot.
    support_bot_token: SecretStr
    # Telegram numeric IDs of the operator(s) who receive and answer tickets.
    # Same people as the main bot's admin_ids. JSON list in env: ADMIN_IDS=[111,222]
    admin_ids: list[int]
    # Persistent sqlite (tickets + messages + admin-message map).
    db_path: str = "/data/support.db"
    # Main bot's sqlite, mounted READ-ONLY — source of a user's initial language.
    main_db_path: str = "/main-data/aegis.db"
    # Set on the HA standby/primary to the shared PostgreSQL URL. The connection
    # is held for the lifetime of polling and acts as a cross-host singleton.
    leader_database_url: str | None = None
    leader_retry_seconds: float = 5.0
    log_level: str = "INFO"
    # Tickets per page in "My tickets".
    page_size: int = 5

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()  # type: ignore[call-arg]
