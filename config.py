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

    # Optional automatic USD -> Toman rate source (generic JSON API you choose). If empty, set the rate
    # manually in the dashboard (Pricing page).
    rate_api_url: str = ""
    rate_api_key: str = ""
    rate_api_key_header: str = "Authorization"
    rate_api_key_prefix: str = "Bearer"
    rate_json_path: str = ""
    rate_unit: str = "IRT"

    # Zarinpal wallet top-up (OFF by default; also needs the dashboard switch). Secrets only via environment.
    zarinpal_merchant_id: str = ""
    zarinpal_sandbox: bool = False       # sandbox.zarinpal.com; refused when APP_ENV=production

    # ---- personal-test switches (all optional; an unset ENV leaves the dashboard setting in charge) ----
    testing_mode: bool = False                 # TESTING_MODE=true: purchases/payments only for TESTER_USER_IDS
    tester_user_ids: str = ""                  # TESTER_USER_IDS=123456789,987654321 (numeric Telegram IDs)
    zarinpal_enabled: bool | None = None       # ZARINPAL_ENABLED     -> overrides setting zarinpal_topup_enabled
    wallet_payments_enabled: bool | None = None  # WALLET_PAYMENTS_ENABLED -> overrides setting wallet_payment_enabled
    custom_stars_enabled: bool | None = None   # CUSTOM_STARS_ENABLED -> overrides setting custom_stars_enabled
    manual_stars_delivery: bool = True         # MANUAL_STARS_DELIVERY=false => Stars cannot be ordered unless an auto sender exists
    auto_stars_delivery: bool = True           # AUTO_STARS_DELIVERY=false => never use an automatic sender (none exists today)
    zarinpal_callback_base_url: str = ""       # optional public URL for Zarinpal's return page (ngrok / Railway); default PUBLIC_BASE_URL

    enable_mock_payment: bool = False   # local development only; ignored in production

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
