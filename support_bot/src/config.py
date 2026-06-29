from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Token of the support bot (e.g. @AegisVPNsupportBot). NEVER commit it — it
    # is read from the environment / a gitignored .env, exactly like the main bot.
    support_bot_token: SecretStr
    # Telegram numeric IDs of the operator(s) who receive and answer tickets.
    # Same people as the main bot's admin_ids. JSON list in env: ADMIN_IDS=[111,222]
    admin_ids: list[int]
    # Persistent sqlite mapping forwarded-message -> originating user.
    db_path: str = "/data/support.db"
    log_level: str = "INFO"

    # Shown to a user on /start. No emoji (house style).
    welcome_text: str = (
        "Это поддержка AegisVPN. Напишите сюда свой вопрос — "
        "оператор увидит сообщение и ответит вам здесь же."
    )
    # Confirmation shown to the user after their message is relayed to operators.
    received_text: str = "Сообщение передано в поддержку. Ожидайте ответа здесь."

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()  # type: ignore[call-arg]
