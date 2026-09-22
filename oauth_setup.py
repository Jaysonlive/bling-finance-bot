from __future__ import annotations

import asyncio
import secrets
from urllib.parse import parse_qs, urlparse

from bling import BlingAuthError, BlingClient
from config import ConfigError, Settings


def extract_code_and_state(value: str) -> tuple[str, str | None]:
    value = value.strip()
    if not value:
        raise ValueError("Entrada vazia.")

    # Accept either the complete callback URL or only the authorization code.
    if "://" not in value and "?" not in value:
        return value, None

    parsed = urlparse(value)
    params = parse_qs(parsed.query)
    code = (params.get("code") or [""])[0].strip()
    state = (params.get("state") or [None])[0]
    if not code:
        raise ValueError("A URL informada não contém o parâmetro 'code'.")
    return code, state


async def run() -> None:
    settings = Settings.from_env()
    state = secrets.token_urlsafe(24)
    client = BlingClient(
        client_id=settings.bling_client_id,
        client_secret=settings.bling_client_secret,
        token_file=settings.bling_token_file,
    )
    authorize_url = client.build_authorize_url(state)

    print("\n=== Autorização inicial do Bling ===\n")
    print("1) Abra AGORA esta URL no navegador e autorize o aplicativo:\n")
    print(authorize_url)
    print(
        "\n2) O Bling redirecionará para a URL cadastrada no aplicativo. "
        "Mesmo que a página não abra, copie a URL completa da barra do navegador."
    )
    print(
        "3) Cole abaixo a URL completa de retorno. O authorization_code do Bling "
        "expira rapidamente, então faça esta etapa sem demora.\n"
    )

    callback = input("URL de retorno (ou somente o code): ").strip()
    code, returned_state = extract_code_and_state(callback)

    if returned_state is not None and returned_state != state:
        raise BlingAuthError(
            "O parâmetro state retornado não corresponde ao state enviado. "
            "Operação interrompida por segurança."
        )

    try:
        await client.bootstrap_authorization_code(code)
    finally:
        await client.close()

    print(f"\nOK: tokens gravados em {settings.bling_token_file}")
    print("O bot já pode consultar a API do Bling.")


def main() -> None:
    try:
        asyncio.run(run())
    except (ConfigError, BlingAuthError, ValueError) as exc:
        raise SystemExit(f"Erro: {exc}") from exc


if __name__ == "__main__":
    main()
