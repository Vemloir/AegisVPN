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
    log_level: str = "INFO"
    # Tickets per page in "My tickets".
    page_size: int = 5

    # Shown on /start and the main menu. No emoji (house style).
    welcome_text: str = (
        "Это поддержка AegisVPN. Здесь можно создать тикет и переписываться с поддержкой.\n\n"
        "«Создать тикет» — новое обращение.\n"
        "«Мои тикеты» — список обращений и переписка."
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()  # type: ignore[call-arg]
