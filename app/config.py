"""Environment configuration. Secrets are read ONLY from environment variables."""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    app_env: str = "development"
    bot_token: str = ""
    mongodb_uri: str = Field(
        default="mongodb://localhost:27017",
        validation_alias=AliasChoices("MONGODB_URI", "MONGO_URL"),
    )
    mongodb_db: str = "telegram_market"
    public_base_url: str = ""
    railway_public_domain: str = ""
    webhook_secret: str = ""
    session_secret: str = ""
    admin_alert_chat_ids: str = ""
    admin_bootstrap_username: str = "owner"
    admin_bootstrap_password: str = ""

    price_provider: str = "http_json"
    price_api_url: str = ""
    price_api_key: str = ""
    price_api_key_header: str = "Authorization"
    price_api_key_prefix: str = "Bearer"
    price_json_path: str = ""
    price_currency: str = "USD"

    rate_provider: str = "http_json"
    rate_api_url: str = ""
    rate_api_key: str = ""
    rate_api_key_header: str = "Authorization"
    rate_api_key_prefix: str = "Bearer"
    rate_json_path: str = ""
    rate_unit: str = "IRT"

    enable_mock_payment: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        if self.railway_public_domain:
            return f"https://{self.railway_public_domain}"
        return ""

    @property
    def alert_ids(self) -> list[int]:
        out: list[int] = []
        for part in self.admin_alert_chat_ids.split(","):
            part = part.strip()
            if part.lstrip("-").isdigit():
                out.append(int(part))
        return out


@lru_cache
def get_config() -> Config:
    return Config()
