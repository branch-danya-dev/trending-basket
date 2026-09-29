"""Application configuration: environment variables and .env, validated strictly."""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PREFIX = "TB_"


class Settings(BaseSettings):
    """Runtime configuration for trending-basket, read from TB_* environment variables."""

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX, env_file=".env", extra="forbid")

    mode: Literal["research", "paper", "demo", "live"] = "research"
    data_dir: Path = Path("./data")
    reports_dir: Path = Path("./reports")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    bybit_api_key: SecretStr | None = None
    bybit_api_secret: SecretStr | None = None
    bybit_rest_url: str = "https://api.bybit.com"
    bybit_demo_rest_url: str = "https://api-demo.bybit.com"
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    demo_risk_file: Path = Path("docs/reports/T006-risk-level.json")
    allocated_capital_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    rest_timeout_s: float = 10.0
    rest_min_interval_s: float = 0.2
    rest_max_retries: int = 6


def _known_env_keys() -> set[str]:
    return {f"{_ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}


def _present_env_keys() -> set[str]:
    """Return TB_*-prefixed keys set in the process environment or in .env."""
    keys = {key for key in os.environ if key.startswith(_ENV_PREFIX)}

    env_file = Path(".env")
    if env_file.is_file():
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if key.startswith(_ENV_PREFIX):
                keys.add(key)

    return keys


def _unknown_keys_error(unknown_keys: list[str], known_keys: list[str]) -> str:
    lines = ["unknown configuration keys:"]
    for key in unknown_keys:
        suggestion = difflib.get_close_matches(key, known_keys, n=1)
        hint = f" (did you mean {suggestion[0]}?)" if suggestion else ""
        lines.append(f"  - {key}{hint}")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Load Settings, rejecting unknown TB_* keys and the disabled live mode."""
    known_keys = sorted(_known_env_keys())
    unknown_keys = sorted(_present_env_keys() - set(known_keys))
    if unknown_keys:
        raise ValueError(_unknown_keys_error(unknown_keys, known_keys))

    settings = Settings()

    if settings.mode == "live":
        raise ValueError("live mode is disabled until roadmap task T010")

    return settings


def format_settings(settings: Settings) -> str:
    """Render settings for display, masking secrets."""

    def mask(secret: SecretStr | None) -> str:
        return "***" if secret is not None else "<unset>"

    lines = [
        f"mode={settings.mode}",
        f"data_dir={settings.data_dir}",
        f"reports_dir={settings.reports_dir}",
        f"log_level={settings.log_level}",
        f"bybit_api_key={mask(settings.bybit_api_key)}",
        f"bybit_api_secret={mask(settings.bybit_api_secret)}",
        f"bybit_rest_url={settings.bybit_rest_url}",
        f"bybit_demo_rest_url={settings.bybit_demo_rest_url}",
        f"telegram_bot_token={mask(settings.telegram_bot_token)}",
        f"telegram_chat_id={mask(settings.telegram_chat_id)}",
        f"demo_risk_file={settings.demo_risk_file}",
        f"allocated_capital_usd={settings.allocated_capital_usd}",
        f"rest_timeout_s={settings.rest_timeout_s}",
        f"rest_min_interval_s={settings.rest_min_interval_s}",
        f"rest_max_retries={settings.rest_max_retries}",
    ]
    return "\n".join(lines)
