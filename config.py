from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Variável de ambiente obrigatória ausente: {name}")
    return value


def _parse_iso_date(name: str, raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} deve estar no formato YYYY-MM-DD.") from exc


def _parse_positive_int(name: str, raw: str, default: int) -> int:
    value = (raw or str(default)).strip()
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} deve ser um número inteiro positivo.") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} deve ser maior que zero.")
    return parsed


def _parse_allowed_users(raw: str) -> FrozenSet[int]:
    values = raw.replace(";", ",").split(",")
    users: set[int] = set()
    for item in values:
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ConfigError("ALLOWED_USERS deve conter somente IDs numéricos separados por vírgula.") from exc
        if user_id <= 0:
            raise ConfigError("Os IDs em ALLOWED_USERS devem ser números positivos.")
        users.add(user_id)
    if not users:
        raise ConfigError("ALLOWED_USERS deve conter ao menos um ID do Telegram.")
    return frozenset(users)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    bling_client_id: str
    bling_client_secret: str
    allowed_users: FrozenSet[int]
    bling_token_file: Path
    cash_history_start: date
    cash_db_file: Path
    cash_bootstrap_days: int
    cash_sync_days: int
    cash_account_discovery_days: int
    timezone: str
    log_level: str
    openai_api_key: str | None
    openai_model: str
    openai_base_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token_file = Path(os.getenv("BLING_TOKEN_FILE", "/app/data/bling_tokens.json").strip())
        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            bling_client_id=_require("BLING_CLIENT_ID"),
            bling_client_secret=_require("BLING_CLIENT_SECRET"),
            allowed_users=_parse_allowed_users(_require("ALLOWED_USERS")),
            bling_token_file=token_file,
            cash_history_start=_parse_iso_date(
                "CASH_HISTORY_START",
                os.getenv("CASH_HISTORY_START", "2000-01-01").strip() or "2000-01-01",
            ),
            cash_db_file=Path(os.getenv("CASH_DB_FILE", "/app/data/financeiro.db").strip() or "/app/data/financeiro.db"),
            cash_bootstrap_days=_parse_positive_int("CASH_BOOTSTRAP_DAYS", os.getenv("CASH_BOOTSTRAP_DAYS", "90"), 90),
            cash_sync_days=_parse_positive_int("CASH_SYNC_DAYS", os.getenv("CASH_SYNC_DAYS", "7"), 7),
            cash_account_discovery_days=_parse_positive_int(
                "CASH_ACCOUNT_DISCOVERY_DAYS",
                os.getenv("CASH_ACCOUNT_DISCOVERY_DAYS", "90"),
                90,
            ),
            timezone=os.getenv("TZ", "America/Sao_Paulo").strip() or "America/Sao_Paulo",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            openai_api_key=(os.getenv("OPENAI_API_KEY", "").strip() or None),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna",
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/") or "https://api.openai.com/v1",
        )
