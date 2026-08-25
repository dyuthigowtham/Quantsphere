from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Purpose:    Central application configuration loaded from environment
                variables / .env. Single source of truth — never read
                os.environ directly elsewhere in the app.
    Args:       None (populated by pydantic-settings from the environment).
    Returns:    N/A (Pydantic model).
    Raises:     pydantic.ValidationError: If a required field is missing
                or malformed, or if environment="production" is set with
                insecure/default secrets still in place.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "production"] = "development"

    database_url: str = "postgresql+asyncpg://quantsphere:quantsphere@localhost:5432/quantsphere"

    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # MT5 auto-sync only works for a single local Windows terminal (see
    # app/services/mt5_sync.py) — architecturally incompatible with a hosted
    # multi-user deployment, so it defaults off there.
    mt5_enabled: bool = False

    ollama_base_url: str = "http://localhost:11434"
    ollama_text_model: str = "llama3"
    ollama_vision_model: str = "llava"
    ollama_request_timeout_seconds: float = 600.0

    # MT5 accounts are now linked per-portfolio from the UI (see
    # app/services/mt5_accounts.py) rather than configured statically here.
    # terminal_path is an optional fallback users can leave blank; mt5.initialize()
    # auto-detects an installed terminal when no explicit path is given.
    mt5_terminal_path: str | None = None
    mt5_poll_interval_seconds: int = 15
    mt5_encryption_key: str | None = None

    media_root: Path = Path("./media")
    max_upload_mb: int = 8

    # Account emails (welcome on signup, password reset) — provider-agnostic
    # SMTP, works with Gmail/Outlook/any mailbox or transactional SMTP
    # relay. Left unset by default: app/services/email.py treats a blank
    # smtp_host as "email disabled" and skips sending rather than failing,
    # so this is opt-in and never blocks signup/login on its own.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    # What recipients see in the "From" field — falls back to smtp_username
    # (the usual case: sending "as" the mailbox you authenticate with) if left blank.
    smtp_from_email: str = ""
    smtp_from_name: str = "QuantSphere"

    @property
    def email_enabled(self) -> bool:
        """
        Purpose:    Single source of truth for "should we attempt to send
                    account emails at all" — every call site checks this
                    instead of re-deriving it from smtp_host directly.
        Args:       None.
        Returns:    bool: True once an SMTP host has been configured.
        Raises:     None.
        """
        return bool(self.smtp_host)

    # Free, no-API-key Yahoo Finance public chart endpoint — no signup needed.
    # EURUSD/GBPUSD/USDJPY/USDCHF/USDCAD/AUDUSD/NZDUSD are the seven forex "majors".
    market_data_default_symbols: list[str] = [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "USDCAD",
        "AUDUSD",
        "NZDUSD",
        "XAUUSD",
        "BTCUSD",
        "NIFTY",
        "SENSEX",
    ]
    market_data_poll_interval_seconds: float = 5.0

    @property
    def screenshot_dir(self) -> Path:
        """
        Purpose:    Resolve the directory trade screenshots are written to.
        Args:       None.
        Returns:    Path: media_root/trade_screenshots.
        Raises:     None.
        """
        return self.media_root / "trade_screenshots"

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """
        Purpose:    Fail fast at startup rather than silently booting a
                    production deployment with a dev-default secret or DB
                    credential — those defaults are fine for local dev but
                    must never reach a real deployment.
        Args:       None.
        Returns:    Settings: self, unchanged.
        Raises:     ValueError: If environment="production" and jwt_secret
                        is unset, or database_url still has dev-default
                        credentials.
        """
        if self.environment == "production":
            if not self.jwt_secret or len(self.jwt_secret) < 16 or self.jwt_secret == "dev-only-insecure-secret-change-me":
                raise ValueError(
                    "jwt_secret must be set to a real secret (16+ chars) when environment=production. "
                    "Generate one with: openssl rand -hex 32"
                )
            if "quantsphere:quantsphere@localhost" in self.database_url:
                raise ValueError("database_url still has the dev-default credentials — set a real one for production.")
        return self


settings = Settings()
