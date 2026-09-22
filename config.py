from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing or invalid."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Variável de ambiente obrigatória ausente: {name}")
    return value


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
            raise ConfigError(
                "ALLOWED_USERS deve conter somente IDs numéricos separados por vírgula."
            ) from exc
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
    timezone: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()

        token_file = Path(
            os.getenv("BLING_TOKEN_FILE", "/app/data/bling_tokens.json").strip()
        )

        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            bling_client_id=_require("BLING_CLIENT_ID"),
            bling_client_secret=_require("BLING_CLIENT_SECRET"),
            allowed_users=_parse_allowed_users(_require("ALLOWED_USERS")),
            bling_token_file=token_file,
            timezone=os.getenv("TZ", "America/Sao_Paulo").strip() or "America/Sao_Paulo",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
