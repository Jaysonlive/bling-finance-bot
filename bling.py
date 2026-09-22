from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


class BlingError(RuntimeError):
    """Base exception for Bling integration errors."""


class BlingAuthError(BlingError):
    """Authentication/authorization problem that requires user attention."""


class BlingAPIError(BlingError):
    """Unexpected response from the Bling REST API."""


@dataclass(slots=True)
class TokenData:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"
    scope: str = ""

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "TokenData":
        try:
            return cls(
                access_token=str(raw["access_token"]),
                refresh_token=str(raw["refresh_token"]),
                expires_at=float(raw["expires_at"]),
                token_type=str(raw.get("token_type", "Bearer")),
                scope=str(raw.get("scope", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BlingAuthError("Arquivo de tokens do Bling inválido ou incompleto.") from exc


@dataclass(frozen=True, slots=True)
class AccountSummary:
    count: int
    total: Decimal


@dataclass(frozen=True, slots=True)
class FinancialSummary:
    start: date
    end: date
    receivable: AccountSummary
    payable: AccountSummary

    @property
    def net(self) -> Decimal:
        return self.receivable.total - self.payable.total


@dataclass(frozen=True, slots=True)
class CashMovement:
    id: str
    account_id: str
    account_name: str
    movement_date: date | None
    direction: str
    amount: Decimal
    description: str

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.direction == "C" else -self.amount


@dataclass(frozen=True, slots=True)
class CashAccountBalance:
    account_id: str
    description: str
    balance: Decimal
    credits: Decimal
    debits: Decimal
    movement_count: int


@dataclass(frozen=True, slots=True)
class CashSummary:
    accounts: tuple[CashAccountBalance, ...]
    movements: tuple[CashMovement, ...]

    @property
    def total_balance(self) -> Decimal:
        return sum((account.balance for account in self.accounts), Decimal("0"))


class BlingClient:
    """
    Async Bling API v3 client.

    Key behaviors:
    - OAuth 2.0 Authorization Code / rotating Refresh Token.
    - JWT opt-in (`enable-jwt: 1`) on token and API requests.
    - Token persistence with atomic file replacement.
    - Async lock around token refresh.
    - 5-minute refresh safety window.
    - Automatic retry after HTTP 401 and transient 429/5xx errors.
    - Pagination with 100 records per page.
    - Internal throttling below Bling's 3 requests/second account limit.
    """

    API_BASE_URL = "https://api.bling.com.br/Api/v3"
    TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"
    AUTHORIZE_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"

    PAGE_SIZE = 100
    TOKEN_EARLY_REFRESH_SECONDS = 300
    # 0.38s between requests ~= 2.63 req/s, leaving margin below 3 req/s.
    MIN_REQUEST_INTERVAL_SECONDS = 0.38
    MAX_RETRIES = 3
    CASH_CACHE_SECONDS = 300

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: str | Path,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        self._tokens: TokenData | None = None
        self._token_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._cash_cache: CashSummary | None = None
        self._cash_cache_at = 0.0
        self._cash_cache_lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self.API_BASE_URL,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "enable-jwt": "1",
                "User-Agent": "bling-finance-telegram-bot/1.0",
            },
        )

    def build_authorize_url(self, state: str) -> str:
        """Build the Bling OAuth authorization URL for a caller-generated state."""
        state = state.strip()
        if not state:
            raise ValueError("OAuth state não pode ser vazio.")
        return (
            f"{self.AUTHORIZE_URL}?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self.client_id,
                    "state": state,
                }
            )
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def load_tokens(self) -> bool:
        """Load persisted tokens. Returns False if the token file does not exist."""
        async with self._token_lock:
            if not self.token_file.exists():
                self._tokens = None
                return False
            self._tokens = await asyncio.to_thread(self._read_token_file)
            return True

    def _read_token_file(self) -> TokenData:
        try:
            with self.token_file.open("r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except FileNotFoundError as exc:
            raise BlingAuthError(
                f"Arquivo de tokens não encontrado em {self.token_file}."
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BlingAuthError(
                f"Não foi possível ler o arquivo de tokens em {self.token_file}."
            ) from exc
        return TokenData.from_json(payload)

    async def _save_tokens(self, tokens: TokenData) -> None:
        await asyncio.to_thread(self._save_tokens_sync, tokens)

    def _save_tokens_sync(self, tokens: TokenData) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_file.with_suffix(self.token_file.suffix + ".tmp")
        data = asdict(tokens)

        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                # chmod may not be supported on every host filesystem.
                pass
            os.replace(tmp, self.token_file)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _tokens_from_response(payload: dict[str, Any], previous_refresh: str | None = None) -> TokenData:
        try:
            access_token = str(payload["access_token"])
            refresh_token = str(payload.get("refresh_token") or previous_refresh or "")
            expires_in = int(payload["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BlingAuthError("Resposta de token do Bling incompleta.") from exc

        if not refresh_token:
            raise BlingAuthError("O Bling não retornou um refresh_token válido.")

        return TokenData(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + max(expires_in, 1),
            token_type=str(payload.get("token_type", "Bearer")),
            scope=str(payload.get("scope", "")),
        )

    async def bootstrap_authorization_code(self, code: str) -> TokenData:
        """Exchange the first authorization_code and persist the returned tokens."""
        code = code.strip()
        if not code:
            raise BlingAuthError("Authorization code vazio.")

        async with self._token_lock:
            payload = await self._token_request(
                {"grant_type": "authorization_code", "code": code}
            )
            tokens = self._tokens_from_response(payload)
            await self._save_tokens(tokens)
            self._tokens = tokens
            return tokens

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        await self._throttle()
        try:
            response = await self._http.post(
                self.TOKEN_URL,
                data=form,
                auth=httpx.BasicAuth(self.client_id, self.client_secret),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "enable-jwt": "1",
                },
            )
        except httpx.RequestError as exc:
            raise BlingAuthError(f"Falha de rede ao autenticar no Bling: {exc}") from exc

        if response.is_error:
            detail = self._extract_error_message(response)
            raise BlingAuthError(
                f"Falha no OAuth do Bling (HTTP {response.status_code}): {detail}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise BlingAuthError("O Bling retornou uma resposta OAuth inválida.") from exc

    async def _ensure_access_token(self) -> str:
        if self._tokens is None:
            if self.token_file.exists():
                # Load lazily; startup may intentionally occur before OAuth bootstrap.
                async with self._token_lock:
                    if self._tokens is None:
                        self._tokens = await asyncio.to_thread(self._read_token_file)
            else:
                raise BlingAuthError(
                    "Tokens do Bling ainda não foram configurados. Use /autorizar no Telegram."
                )

        assert self._tokens is not None
        if time.time() + self.TOKEN_EARLY_REFRESH_SECONDS >= self._tokens.expires_at:
            await self.refresh_access_token()

        assert self._tokens is not None
        return self._tokens.access_token

    async def refresh_access_token(self, *, force: bool = False) -> TokenData:
        """Refresh OAuth tokens safely; the newly rotated refresh token is persisted."""
        async with self._token_lock:
            if self._tokens is None:
                self._tokens = await asyncio.to_thread(self._read_token_file)

            assert self._tokens is not None
            if (
                not force
                and time.time() + self.TOKEN_EARLY_REFRESH_SECONDS < self._tokens.expires_at
            ):
                return self._tokens

            old_refresh = self._tokens.refresh_token
            payload = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": old_refresh}
            )
            tokens = self._tokens_from_response(payload, previous_refresh=old_refresh)

            # Persist BEFORE exposing the new in-memory token. This matters for rotating
            # refresh tokens: losing the new token after a restart could break reauth.
            await self._save_tokens(tokens)
            self._tokens = tokens
            logger.info("Tokens OAuth do Bling renovados e persistidos com sucesso.")
            return tokens

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = self.MIN_REQUEST_INTERVAL_SECONDS - (now - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _extract_error_message(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            text = response.text.strip()
            return text[:500] if text else "sem detalhes"

        if isinstance(payload, dict):
            error = payload.get("error", payload)
            if isinstance(error, dict):
                parts = [
                    str(error.get(key, "")).strip()
                    for key in ("type", "message", "description")
                ]
                parts = [part for part in parts if part]
                if parts:
                    return " | ".join(parts)[:800]
            return json.dumps(payload, ensure_ascii=False)[:800]
        return str(payload)[:800]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        refreshed_after_401 = False

        for attempt in range(self.MAX_RETRIES + 1):
            access_token = await self._ensure_access_token()
            headers = {
                "Authorization": f"Bearer {access_token}",
                "enable-jwt": "1",
            }

            await self._throttle()
            try:
                response = await self._http.request(
                    method,
                    path,
                    params=params,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.MAX_RETRIES:
                    raise BlingAPIError(f"Falha de rede ao consultar o Bling: {exc}") from exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401 and not refreshed_after_401:
                logger.warning("Bling retornou HTTP 401; forçando renovação do token.")
                await self.refresh_access_token(force=True)
                refreshed_after_401 = True
                continue

            if response.status_code == 429:
                detail = self._extract_error_message(response)
                try:
                    payload = response.json()
                    error = payload.get("error", {}) if isinstance(payload, dict) else {}
                    period = error.get("period") if isinstance(error, dict) else None
                except ValueError:
                    period = None

                if period == "day" or attempt >= self.MAX_RETRIES:
                    raise BlingAPIError(f"Limite da API Bling atingido: {detail}")

                await asyncio.sleep(1.5 * (2**attempt))
                continue

            if response.status_code >= 500:
                if attempt >= self.MAX_RETRIES:
                    raise BlingAPIError(
                        f"Bling indisponível (HTTP {response.status_code}): "
                        f"{self._extract_error_message(response)}"
                    )
                await asyncio.sleep(1.5 * (2**attempt))
                continue

            if response.is_error:
                detail = self._extract_error_message(response)
                if response.status_code in (401, 403):
                    raise BlingAuthError(
                        f"Acesso ao Bling negado (HTTP {response.status_code}): {detail}"
                    )
                raise BlingAPIError(
                    f"Erro da API Bling (HTTP {response.status_code}): {detail}"
                )

            if response.status_code == 204:
                return {}

            try:
                payload = response.json()
            except ValueError as exc:
                raise BlingAPIError("O Bling retornou JSON inválido.") from exc

            if not isinstance(payload, dict):
                raise BlingAPIError("Formato inesperado na resposta do Bling.")
            return payload

        raise BlingAPIError("Falha inesperada após múltiplas tentativas na API Bling.")

    async def _paginate(
        self,
        path: str,
        base_params: Sequence[tuple[str, Any]],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1

        while True:
            params = list(base_params)
            params.extend((("pagina", page), ("limite", self.PAGE_SIZE)))
            payload = await self._request("GET", path, params=params)
            data = payload.get("data", [])

            if data is None:
                data = []
            if not isinstance(data, list):
                raise BlingAPIError(
                    f"Resposta paginada inesperada em {path}: campo 'data' não é lista."
                )

            rows = [row for row in data if isinstance(row, dict)]
            results.extend(rows)

            if len(data) < self.PAGE_SIZE:
                break

            page += 1
            if page > 10000:
                raise BlingAPIError("Paginação interrompida por limite de segurança.")

        return results

    @staticmethod
    def _split_date_range(start: date, end: date) -> Iterable[tuple[date, date]]:
        if end < start:
            raise ValueError("A data final não pode ser anterior à data inicial.")

        # Bling rejects filtered intervals longer than one year. Using 365-day
        # inclusive chunks keeps every request safely below that boundary.
        cursor = start
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=364), end)
            yield cursor, chunk_end
            cursor = chunk_end + timedelta(days=1)

    @staticmethod
    def _deduplicate(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id: dict[Any, dict[str, Any]] = {}
        without_id: list[dict[str, Any]] = []
        for row in rows:
            row_id = row.get("id")
            if row_id is None:
                without_id.append(row)
            else:
                by_id[row_id] = row
        return list(by_id.values()) + without_id

    async def list_open_receivables(self, start: date, end: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk_start, chunk_end in self._split_date_range(start, end):
            params: list[tuple[str, Any]] = [
                ("situacoes[]", 1),
                ("situacoes[]", 3),
                ("tipoFiltroData", "V"),
                ("dataInicial", chunk_start.isoformat()),
                ("dataFinal", chunk_end.isoformat()),
            ]
            rows.extend(await self._paginate("/contas/receber", params))
        return self._deduplicate(rows)

    async def list_open_payables(self, start: date, end: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for chunk_start, chunk_end in self._split_date_range(start, end):
            # The current payable endpoint accepts a single `situacao` value,
            # so open (1) and partial (3) are paginated independently.
            for status in (1, 3):
                params: list[tuple[str, Any]] = [
                    ("dataVencimentoInicial", chunk_start.isoformat()),
                    ("dataVencimentoFinal", chunk_end.isoformat()),
                    ("situacao", status),
                ]
                rows.extend(await self._paginate("/contas/pagar", params))
        return self._deduplicate(rows)

    async def _get_account_detail(self, kind: str, account_id: Any) -> dict[str, Any]:
        if kind not in {"receber", "pagar"}:
            raise ValueError("kind inválido")
        payload = await self._request("GET", f"/contas/{kind}/{account_id}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BlingAPIError(
                f"Detalhe da conta {kind} {account_id} retornou formato inesperado."
            )
        return data

    @staticmethod
    def _to_decimal(value: Any) -> Decimal:
        if value is None or value == "":
            return Decimal("0")
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise BlingAPIError(f"Valor financeiro inválido retornado pelo Bling: {value!r}") from exc

    async def _summarize_rows(self, kind: str, rows: list[dict[str, Any]]) -> AccountSummary:
        total = Decimal("0")

        for row in rows:
            status = row.get("situacao")
            if status == 3:
                account_id = row.get("id")
                if account_id is None:
                    raise BlingAPIError("Conta parcial retornada sem ID.")
                detail = await self._get_account_detail(kind, account_id)
                amount = self._to_decimal(detail.get("saldo"))
            else:
                # For a fully open account, outstanding balance equals face value.
                amount = self._to_decimal(row.get("saldo", row.get("valor")))
            total += amount

        return AccountSummary(count=len(rows), total=total)

    async def get_receivable_summary(self, start: date, end: date) -> AccountSummary:
        rows = await self.list_open_receivables(start, end)
        return await self._summarize_rows("receber", rows)

    async def get_payable_summary(self, start: date, end: date) -> AccountSummary:
        rows = await self.list_open_payables(start, end)
        return await self._summarize_rows("pagar", rows)

    async def get_financial_summary(self, start: date, end: date) -> FinancialSummary:
        receivable = await self.get_receivable_summary(start, end)
        payable = await self.get_payable_summary(start, end)
        return FinancialSummary(
            start=start,
            end=end,
            receivable=receivable,
            payable=payable,
        )

    @staticmethod
    def _first_value(row: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in row and row[key] not in (None, ""):
                return row[key]
        return None

    @staticmethod
    def _nested_value(row: dict[str, Any], object_keys: Sequence[str], field: str) -> Any:
        for object_key in object_keys:
            value = row.get(object_key)
            if isinstance(value, dict) and value.get(field) not in (None, ""):
                return value.get(field)
        return None

    @staticmethod
    def _parse_api_date(value: Any) -> date | None:
        if value in (None, ""):
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None

    async def list_cash_entries(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict[str, Any]]:
        """List Caixas e Bancos movements using Bling's paginated GET /caixas.

        With no dates, Bling returns the account history page by page. Date filters are
        used only when both bounds are provided. Current Bling versions accept ISO
        YYYY-MM-DD for dataInicial/dataFinal.
        """
        if (start is None) != (end is None):
            raise ValueError("Informe data inicial e final juntas.")
        if start is not None and end is not None and end < start:
            raise ValueError("A data final não pode ser anterior à data inicial.")

        rows: list[dict[str, Any]] = []
        ranges: Iterable[tuple[date | None, date | None]]
        if start is None:
            ranges = ((None, None),)
        else:
            assert end is not None
            ranges = self._split_date_range(start, end)

        for chunk_start, chunk_end in ranges:
            params: list[tuple[str, Any]] = []
            if chunk_start is not None and chunk_end is not None:
                params.extend(
                    (
                        ("dataInicial", chunk_start.isoformat()),
                        ("dataFinal", chunk_end.isoformat()),
                    )
                )
            rows.extend(await self._paginate("/caixas", params))
        return rows

    def _cash_movement_from_row(self, row: dict[str, Any]) -> CashMovement | None:
        direction = str(
            self._first_value(row, "debcred", "debCred", "debitoCredito") or ""
        ).strip().upper()
        if direction not in {"C", "D"}:
            logger.warning(
                "Ignorando lançamento de caixa sem indicador C/D reconhecido: id=%s",
                row.get("id"),
            )
            return None

        amount = abs(self._to_decimal(row.get("valor")))
        account_id_value = self._first_value(
            row,
            "contafinanceira_id",
            "contaFinanceiraId",
            "conta_financeira_id",
        )
        if account_id_value in (None, ""):
            account_id_value = self._nested_value(
                row, ("contaFinanceira", "contafinanceira", "conta_financeira"), "id"
            )

        account_name_value = self._first_value(
            row,
            "contafinanceira_descricao",
            "contaFinanceiraDescricao",
            "conta_financeira_descricao",
        )
        if account_name_value in (None, ""):
            account_name_value = self._nested_value(
                row,
                ("contaFinanceira", "contafinanceira", "conta_financeira"),
                "descricao",
            )

        account_id = str(account_id_value or "sem-conta").strip() or "sem-conta"
        account_name = str(account_name_value or "Sem conta financeira").strip()
        movement_id = str(row.get("id") or "").strip()
        description = str(
            self._first_value(row, "descricao", "observacoes", "historico") or ""
        ).strip()

        return CashMovement(
            id=movement_id,
            account_id=account_id,
            account_name=account_name,
            movement_date=self._parse_api_date(row.get("data")),
            direction=direction,
            amount=amount,
            description=description,
        )

    async def get_cash_summary(self, *, force_refresh: bool = False) -> CashSummary:
        """Calculate balances from all movements exposed by GET /caixas.

        Bling's list response contains debit/credit, value, date and financial account.
        Credits increase the account balance and debits reduce it. The result represents
        the balance recorded in Bling, not a live balance queried from the bank.
        """
        now = time.monotonic()
        if (
            not force_refresh
            and self._cash_cache is not None
            and now - self._cash_cache_at < self.CASH_CACHE_SECONDS
        ):
            return self._cash_cache

        async with self._cash_cache_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._cash_cache is not None
                and now - self._cash_cache_at < self.CASH_CACHE_SECONDS
            ):
                return self._cash_cache

            rows = await self.list_cash_entries()
            movements: list[CashMovement] = []
            buckets: dict[str, dict[str, Any]] = {}

            for row in rows:
                movement = self._cash_movement_from_row(row)
                if movement is None:
                    continue
                movements.append(movement)
                bucket = buckets.setdefault(
                    movement.account_id,
                    {
                        "description": movement.account_name,
                        "credits": Decimal("0"),
                        "debits": Decimal("0"),
                        "count": 0,
                    },
                )
                # Prefer a real name if the first row had no account description.
                if (
                    bucket["description"] == "Sem conta financeira"
                    and movement.account_name != "Sem conta financeira"
                ):
                    bucket["description"] = movement.account_name
                if movement.direction == "C":
                    bucket["credits"] += movement.amount
                else:
                    bucket["debits"] += movement.amount
                bucket["count"] += 1

            accounts = tuple(
                sorted(
                    (
                        CashAccountBalance(
                            account_id=account_id,
                            description=str(bucket["description"]),
                            balance=bucket["credits"] - bucket["debits"],
                            credits=bucket["credits"],
                            debits=bucket["debits"],
                            movement_count=int(bucket["count"]),
                        )
                        for account_id, bucket in buckets.items()
                    ),
                    key=lambda account: account.description.casefold(),
                )
            )

            movements.sort(
                key=lambda movement: (
                    movement.movement_date or date.min,
                    movement.id,
                ),
                reverse=True,
            )
            summary = CashSummary(accounts=accounts, movements=tuple(movements))
            self._cash_cache = summary
            self._cash_cache_at = time.monotonic()
            return summary

    async def get_cash_account(
        self,
        account_id: str,
        *,
        force_refresh: bool = False,
    ) -> tuple[CashAccountBalance, tuple[CashMovement, ...]]:
        summary = await self.get_cash_summary(force_refresh=force_refresh)
        account = next(
            (item for item in summary.accounts if item.account_id == str(account_id)),
            None,
        )
        if account is None:
            raise BlingAPIError("Conta financeira não encontrada nos lançamentos de Caixas e Bancos.")
        movements = tuple(
            movement
            for movement in summary.movements
            if movement.account_id == account.account_id
        )
        return account, movements
