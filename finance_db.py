from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass(frozen=True, slots=True)
class LocalAccount:
    account_id: str
    description: str
    enabled: bool
    manual_enabled: bool | None
    base_date: date | None
    base_balance: Decimal | None
    first_seen: date | None
    last_seen: date | None


@dataclass(frozen=True, slots=True)
class LocalMovement:
    movement_id: str
    account_id: str
    movement_date: date
    direction: str
    amount: Decimal
    category_id: str | None
    category_name: str
    supplier_name: str
    supplier_tax_id: str
    description: str
    affects_balance: bool
    status: str

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.direction == "C" else -self.amount


@dataclass(frozen=True, slots=True)
class LocalBalance:
    account: LocalAccount
    balance: Decimal | None
    credits: Decimal
    debits: Decimal
    movement_count: int


@dataclass(frozen=True, slots=True)
class SyncStatus:
    movements: int
    accounts: int
    enabled_accounts: int
    calibrated_accounts: int
    categories: int
    oldest_date: date | None
    newest_date: date | None
    last_sync_at: datetime | None
    last_sync_start: date | None
    last_sync_end: date | None
    last_category_sync_at: datetime | None


def _to_cents(value: Decimal) -> int:
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(quantized * 100)


def _from_cents(value: int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(value) / Decimal(100)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def normalize_text(value: str | None) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.casefold()
    raw = re.sub(r"[^a-z0-9]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


class FinanceDB:
    """Persistent financial cache and reporting store.

    The Bling API is treated as the source of truth for synchronization, while
    SQLite is the source used by day-to-day reports. Recent periods are replaced
    atomically so edits and deletions in Bling are reflected without rescanning
    years of history.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _ensure_column(self, conn: sqlite3.Connection, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._column_names(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cash_accounts (
                    account_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    manual_enabled INTEGER,
                    base_date TEXT,
                    base_balance_cents INTEGER,
                    first_seen TEXT,
                    last_seen TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS categories (
                    category_id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    category_type INTEGER NOT NULL DEFAULT 0,
                    parent_id TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_categories_parent
                    ON categories(parent_id);
                CREATE INDEX IF NOT EXISTS idx_categories_description
                    ON categories(description COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS category_classifications (
                    category_id TEXT PRIMARY KEY,
                    cost_behavior TEXT NOT NULL DEFAULT 'other',
                    managerial_group TEXT NOT NULL DEFAULT 'other',
                    dre_line TEXT NOT NULL DEFAULT 'auto',
                    is_opex INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(category_id) REFERENCES categories(category_id)
                );

                CREATE TABLE IF NOT EXISTS cash_movements (
                    movement_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    movement_date TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK(direction IN ('C','D')),
                    amount_cents INTEGER NOT NULL,
                    category_id TEXT,
                    category_name TEXT NOT NULL DEFAULT '',
                    supplier_name TEXT NOT NULL DEFAULT '',
                    supplier_tax_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    affects_balance INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'R',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(account_id) REFERENCES cash_accounts(account_id)
                );

                CREATE INDEX IF NOT EXISTS idx_cash_movements_date
                    ON cash_movements(movement_date);
                CREATE INDEX IF NOT EXISTS idx_cash_movements_account_date
                    ON cash_movements(account_id, movement_date);
                CREATE INDEX IF NOT EXISTS idx_cash_movements_category_date
                    ON cash_movements(category_id, movement_date);
                CREATE INDEX IF NOT EXISTS idx_cash_movements_supplier_date
                    ON cash_movements(supplier_name COLLATE NOCASE, movement_date);
                CREATE INDEX IF NOT EXISTS idx_cash_movements_direction_date
                    ON cash_movements(direction, movement_date);

                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_periods (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    records INTEGER NOT NULL,
                    sync_type TEXT NOT NULL,
                    synced_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            # Migration from v6 databases.
            self._ensure_column(conn, "cash_accounts", "manual_enabled INTEGER")
            for definition in (
                "category_id TEXT",
                "category_name TEXT NOT NULL DEFAULT ''",
                "supplier_name TEXT NOT NULL DEFAULT ''",
                "supplier_tax_id TEXT NOT NULL DEFAULT ''",
                "affects_balance INTEGER NOT NULL DEFAULT 1",
                "status TEXT NOT NULL DEFAULT 'R'",
                "raw_json TEXT NOT NULL DEFAULT '{}'",
            ):
                self._ensure_column(conn, "cash_movements", definition)

            defaults = {
                "workdays_per_month": "21",
                "workhours_per_day": "8",
                "anomaly_percent_threshold": "30",
                "anomaly_min_amount": "100",
                "small_expense_threshold": "100",
            }
            now = self._utc_now()
            for key, value in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
                    (key, value, now),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # State / settings
    # ------------------------------------------------------------------
    def _set_state_conn(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO sync_state(key, value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )

    def set_state(self, key: str, value: str) -> None:
        with self._connection() as conn:
            self._set_state_conn(conn, key, value)
            conn.commit()

    def get_state(self, key: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, str(value), self._utc_now()),
            )
            conn.commit()

    def all_settings(self) -> dict[str, str]:
        with self._connection() as conn:
            rows = conn.execute("SELECT key,value FROM app_settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    # ------------------------------------------------------------------
    # Accounts and balance calibration
    # ------------------------------------------------------------------
    def is_empty(self) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM cash_movements").fetchone()
            return not row or int(row["n"]) == 0

    def upsert_account_catalog(self, accounts: Iterable[dict[str, Any]]) -> int:
        now = self._utc_now()
        count = 0
        with self._connection() as conn:
            for item in accounts:
                account_id = str(item.get("account_id") or "").strip()
                description = str(item.get("description") or "").strip()
                if not account_id or not description:
                    continue
                conn.execute(
                    """
                    INSERT INTO cash_accounts(account_id,description,enabled,updated_at)
                    VALUES(?,?,0,?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        description=excluded.description,
                        updated_at=excluded.updated_at
                    """,
                    (account_id, description, now),
                )
                count += 1
            conn.commit()
        return count

    def reconcile_auto_accounts(self, active_ids: Sequence[str]) -> None:
        ids = {str(x) for x in active_ids if str(x)}
        now = self._utc_now()
        with self._connection() as conn:
            rows = conn.execute("SELECT account_id,manual_enabled FROM cash_accounts").fetchall()
            for row in rows:
                if row["manual_enabled"] is not None:
                    enabled = int(row["manual_enabled"])
                else:
                    enabled = 1 if str(row["account_id"]) in ids else 0
                conn.execute(
                    "UPDATE cash_accounts SET enabled=?,updated_at=? WHERE account_id=?",
                    (enabled, now, row["account_id"]),
                )
            conn.commit()

    def replace_period(
        self,
        start: date,
        end: date,
        movements: Iterable[dict[str, Any]],
        *,
        discover_since: date | None = None,
        sync_type: str = "incremental",
    ) -> int:
        """Atomically replace one date range from Bling.

        Replacement is intentional: records removed in Bling disappear locally on
        the next resynchronization of the same period.
        """
        now = self._utc_now()
        rows = list(movements)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM cash_movements WHERE movement_date BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
            for item in rows:
                movement_date: date = item["movement_date"]
                account_id = str(item["account_id"])
                account_name = str(item.get("account_name") or account_id)
                existing = conn.execute(
                    "SELECT first_seen,last_seen,enabled,manual_enabled FROM cash_accounts WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                should_enable = bool(discover_since and movement_date >= discover_since)
                if existing:
                    first_seen = min(filter(None, [_parse_date(existing["first_seen"]), movement_date]))
                    last_seen = max(filter(None, [_parse_date(existing["last_seen"]), movement_date]))
                    enabled = int(existing["enabled"])
                    if existing["manual_enabled"] is None and should_enable:
                        enabled = 1
                    conn.execute(
                        """
                        UPDATE cash_accounts
                           SET description=?,enabled=?,first_seen=?,last_seen=?,updated_at=?
                         WHERE account_id=?
                        """,
                        (
                            account_name,
                            enabled,
                            first_seen.isoformat(),
                            last_seen.isoformat(),
                            now,
                            account_id,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO cash_accounts(
                            account_id,description,enabled,first_seen,last_seen,updated_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (
                            account_id,
                            account_name,
                            1 if should_enable else 0,
                            movement_date.isoformat(),
                            movement_date.isoformat(),
                            now,
                        ),
                    )

                category_id = str(item.get("category_id") or "").strip() or None
                category_name = str(item.get("category_name") or "").strip()
                if category_id is None and category_name:
                    category_row = conn.execute(
                        "SELECT category_id FROM categories WHERE lower(description)=lower(?) LIMIT 1",
                        (category_name,),
                    ).fetchone()
                    if category_row is not None:
                        category_id = str(category_row["category_id"])

                raw_json = item.get("raw_json")
                if not isinstance(raw_json, str):
                    raw_json = json.dumps(raw_json or {}, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    INSERT INTO cash_movements(
                        movement_id,account_id,movement_date,direction,amount_cents,
                        category_id,category_name,supplier_name,supplier_tax_id,
                        description,affects_balance,status,raw_json,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(movement_id) DO UPDATE SET
                        account_id=excluded.account_id,
                        movement_date=excluded.movement_date,
                        direction=excluded.direction,
                        amount_cents=excluded.amount_cents,
                        category_id=excluded.category_id,
                        category_name=excluded.category_name,
                        supplier_name=excluded.supplier_name,
                        supplier_tax_id=excluded.supplier_tax_id,
                        description=excluded.description,
                        affects_balance=excluded.affects_balance,
                        status=excluded.status,
                        raw_json=excluded.raw_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(item["movement_id"]),
                        account_id,
                        movement_date.isoformat(),
                        str(item["direction"]),
                        _to_cents(item["amount"]),
                        category_id,
                        category_name,
                        str(item.get("supplier_name") or ""),
                        str(item.get("supplier_tax_id") or ""),
                        str(item.get("description") or ""),
                        1 if item.get("affects_balance", True) else 0,
                        str(item.get("status") or "R"),
                        raw_json,
                        now,
                    ),
                )

            self._set_state_conn(conn, "last_sync_at", now)
            self._set_state_conn(conn, "last_sync_start", start.isoformat())
            self._set_state_conn(conn, "last_sync_end", end.isoformat())
            conn.execute(
                "INSERT INTO sync_periods(start_date,end_date,records,sync_type,synced_at) VALUES(?,?,?,?,?)",
                (start.isoformat(), end.isoformat(), len(rows), sync_type, now),
            )
            conn.commit()
        return len(rows)

    def list_accounts(self, *, enabled_only: bool = False) -> tuple[LocalAccount, ...]:
        sql = "SELECT * FROM cash_accounts"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY description COLLATE NOCASE"
        with self._connection() as conn:
            rows = conn.execute(sql).fetchall()
        return tuple(self._account_from_row(row) for row in rows)

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> LocalAccount:
        manual_raw = row["manual_enabled"] if "manual_enabled" in row.keys() else None
        return LocalAccount(
            account_id=str(row["account_id"]),
            description=str(row["description"]),
            enabled=bool(row["enabled"]),
            manual_enabled=None if manual_raw is None else bool(manual_raw),
            base_date=_parse_date(row["base_date"]),
            base_balance=_from_cents(row["base_balance_cents"]),
            first_seen=_parse_date(row["first_seen"]),
            last_seen=_parse_date(row["last_seen"]),
        )

    def set_account_enabled(self, query: str, enabled: bool) -> LocalAccount:
        query = query.strip()
        if not query:
            raise ValueError("Informe o nome ou ID da conta.")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cash_accounts WHERE account_id=? OR lower(description)=lower(?)",
                (query, query),
            ).fetchone()
            if row is None:
                # Friendly contains search.
                row = conn.execute(
                    "SELECT * FROM cash_accounts WHERE lower(description) LIKE lower(?) ORDER BY length(description) LIMIT 1",
                    (f"%{query}%",),
                ).fetchone()
            if row is None:
                raise ValueError(f"Conta financeira não encontrada: {query}")
            conn.execute(
                "UPDATE cash_accounts SET enabled=?,manual_enabled=?,updated_at=? WHERE account_id=?",
                (1 if enabled else 0, 1 if enabled else 0, self._utc_now(), row["account_id"]),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM cash_accounts WHERE account_id=?", (row["account_id"],)
            ).fetchone()
        assert updated is not None
        return self._account_from_row(updated)

    def clear_account_override(self, query: str) -> LocalAccount:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM cash_accounts WHERE account_id=? OR lower(description)=lower(?)",
                (query, query),
            ).fetchone()
            if row is None:
                raise ValueError(f"Conta financeira não encontrada: {query}")
            conn.execute(
                "UPDATE cash_accounts SET manual_enabled=NULL,updated_at=? WHERE account_id=?",
                (self._utc_now(), row["account_id"]),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM cash_accounts WHERE account_id=?", (row["account_id"],)
            ).fetchone()
        assert updated is not None
        return self._account_from_row(updated)

    def calibrate_balances(self, balances: dict[str, Decimal], as_of: date) -> tuple[LocalAccount, ...]:
        """Anchor exact sidebar balances without importing the entire history."""
        with self._connection() as conn:
            accounts = conn.execute(
                "SELECT * FROM cash_accounts WHERE enabled=1 ORDER BY description COLLATE NOCASE"
            ).fetchall()
            if not accounts:
                raise ValueError("Nenhuma conta financeira ativa foi detectada.")

            by_name = {normalize_text(str(row["description"])): row for row in accounts}
            by_id = {str(row["account_id"]): row for row in accounts}
            resolved: dict[str, tuple[sqlite3.Row, Decimal]] = {}
            for key, current_balance in balances.items():
                row = by_id.get(str(key)) or by_name.get(normalize_text(str(key)))
                if row is None:
                    raise ValueError(f"Conta ativa não encontrada: {key}")
                resolved[str(row["account_id"])] = (row, current_balance)

            missing = [
                str(row["description"])
                for row in accounts
                if str(row["account_id"]) not in resolved
            ]
            if missing:
                raise ValueError("Informe o saldo de todas as contas ativas. Faltando: " + ", ".join(missing))

            base_date = as_of - timedelta(days=1)
            now = self._utc_now()
            for account_id, (_, current_balance) in resolved.items():
                movement = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN direction='C' AND affects_balance=1 THEN amount_cents ELSE 0 END),0) credits,
                        COALESCE(SUM(CASE WHEN direction='D' AND affects_balance=1 THEN amount_cents ELSE 0 END),0) debits
                    FROM cash_movements WHERE account_id=? AND movement_date=? AND status!='E'
                    """,
                    (account_id, as_of.isoformat()),
                ).fetchone()
                today_net = Decimal(int(movement["credits"]) - int(movement["debits"])) / Decimal(100)
                base_balance = current_balance - today_net
                conn.execute(
                    "UPDATE cash_accounts SET base_date=?,base_balance_cents=?,updated_at=? WHERE account_id=?",
                    (base_date.isoformat(), _to_cents(base_balance), now, account_id),
                )
            conn.commit()
        return self.list_accounts(enabled_only=True)

    def balances_as_of(self, as_of: date) -> tuple[LocalBalance, ...]:
        result: list[LocalBalance] = []
        with self._connection() as conn:
            accounts = conn.execute(
                "SELECT * FROM cash_accounts WHERE enabled=1 ORDER BY description COLLATE NOCASE"
            ).fetchall()
            for row in accounts:
                account = self._account_from_row(row)
                if account.base_date is None or account.base_balance is None:
                    movement = conn.execute(
                        """
                        SELECT
                            COALESCE(SUM(CASE WHEN direction='C' AND affects_balance=1 AND status!='E' THEN amount_cents ELSE 0 END),0) credits,
                            COALESCE(SUM(CASE WHEN direction='D' AND affects_balance=1 AND status!='E' THEN amount_cents ELSE 0 END),0) debits,
                            COUNT(CASE WHEN affects_balance=1 AND status!='E' THEN 1 END) n
                        FROM cash_movements WHERE account_id=? AND movement_date<=?
                        """,
                        (account.account_id, as_of.isoformat()),
                    ).fetchone()
                    result.append(LocalBalance(
                        account=account,
                        balance=None,
                        credits=Decimal(int(movement["credits"])) / Decimal(100),
                        debits=Decimal(int(movement["debits"])) / Decimal(100),
                        movement_count=int(movement["n"] or 0),
                    ))
                    continue

                movement = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN direction='C' AND affects_balance=1 AND status!='E' THEN amount_cents ELSE 0 END),0) credits,
                        COALESCE(SUM(CASE WHEN direction='D' AND affects_balance=1 AND status!='E' THEN amount_cents ELSE 0 END),0) debits,
                        COUNT(CASE WHEN affects_balance=1 AND status!='E' THEN 1 END) n
                    FROM cash_movements
                    WHERE account_id=? AND movement_date>? AND movement_date<=?
                    """,
                    (account.account_id, account.base_date.isoformat(), as_of.isoformat()),
                ).fetchone()
                credits = Decimal(int(movement["credits"])) / Decimal(100)
                debits = Decimal(int(movement["debits"])) / Decimal(100)
                result.append(LocalBalance(
                    account=account,
                    balance=account.base_balance + credits - debits,
                    credits=credits,
                    debits=debits,
                    movement_count=int(movement["n"] or 0),
                ))
        return tuple(result)

    # ------------------------------------------------------------------
    # Categories and editable management classification
    # ------------------------------------------------------------------
    def upsert_categories(self, categories: Iterable[dict[str, Any]]) -> int:
        now = self._utc_now()
        rows = list(categories)
        with self._connection() as conn:
            for item in rows:
                cid = str(item.get("category_id") or "").strip()
                description = str(item.get("description") or "").strip()
                if not cid or not description:
                    continue
                conn.execute(
                    """
                    INSERT INTO categories(category_id,description,category_type,parent_id,active,updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(category_id) DO UPDATE SET
                        description=excluded.description,
                        category_type=excluded.category_type,
                        parent_id=excluded.parent_id,
                        active=excluded.active,
                        updated_at=excluded.updated_at
                    """,
                    (
                        cid,
                        description,
                        int(item.get("category_type") or 0),
                        str(item.get("parent_id") or "") or None,
                        1 if item.get("active", True) else 0,
                        now,
                    ),
                )
            self._set_state_conn(conn, "last_category_sync_at", now)
            conn.commit()
        return len(rows)

    def list_categories(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        sql = """
            SELECT c.*,
                   cc.cost_behavior,cc.managerial_group,cc.dre_line,cc.is_opex,cc.notes
              FROM categories c
              LEFT JOIN category_classifications cc ON cc.category_id=c.category_id
        """
        if active_only:
            sql += " WHERE c.active=1"
        sql += " ORDER BY c.description COLLATE NOCASE"
        with self._connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def resolve_category(self, query: str) -> dict[str, Any] | None:
        needle = normalize_text(query)
        if not needle:
            return None
        categories = self.list_categories(active_only=False)
        exact = [c for c in categories if normalize_text(c["description"]) == needle or str(c["category_id"]) == query.strip()]
        if exact:
            return exact[0]
        contains = [c for c in categories if needle in normalize_text(c["description"])]
        if contains:
            return min(contains, key=lambda c: len(str(c["description"])))
        scored = sorted(
            ((SequenceMatcher(None, needle, normalize_text(c["description"])).ratio(), c) for c in categories),
            key=lambda x: x[0], reverse=True,
        )
        if scored and scored[0][0] >= 0.72:
            return scored[0][1]
        return None

    def category_descendants(self, category_id: str) -> set[str]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                WITH RECURSIVE tree(category_id) AS (
                    SELECT category_id FROM categories WHERE category_id=?
                    UNION ALL
                    SELECT c.category_id FROM categories c JOIN tree t ON c.parent_id=t.category_id
                )
                SELECT category_id FROM tree
                """,
                (str(category_id),),
            ).fetchall()
        return {str(row["category_id"]) for row in rows}

    def set_category_classification(
        self,
        category_query: str,
        *,
        cost_behavior: str | None = None,
        managerial_group: str | None = None,
        dre_line: str | None = None,
        is_opex: bool | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        category = self.resolve_category(category_query)
        if not category:
            raise ValueError(f"Categoria não encontrada: {category_query}")
        cid = str(category["category_id"])
        now = self._utc_now()
        with self._connection() as conn:
            current = conn.execute(
                "SELECT * FROM category_classifications WHERE category_id=?", (cid,)
            ).fetchone()
            values = {
                "cost_behavior": str(current["cost_behavior"] if current else "other"),
                "managerial_group": str(current["managerial_group"] if current else "other"),
                "dre_line": str(current["dre_line"] if current else "auto"),
                "is_opex": int(current["is_opex"] if current else 0),
                "notes": str(current["notes"] if current else ""),
            }
            if cost_behavior is not None:
                values["cost_behavior"] = cost_behavior
            if managerial_group is not None:
                values["managerial_group"] = managerial_group
            if dre_line is not None:
                values["dre_line"] = dre_line
            if is_opex is not None:
                values["is_opex"] = 1 if is_opex else 0
            if notes is not None:
                values["notes"] = notes
            conn.execute(
                """
                INSERT INTO category_classifications(
                    category_id,cost_behavior,managerial_group,dre_line,is_opex,notes,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(category_id) DO UPDATE SET
                    cost_behavior=excluded.cost_behavior,
                    managerial_group=excluded.managerial_group,
                    dre_line=excluded.dre_line,
                    is_opex=excluded.is_opex,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (cid, values["cost_behavior"], values["managerial_group"], values["dre_line"], values["is_opex"], values["notes"], now),
            )
            conn.commit()
        return self.resolve_category(cid) or category


    def resolve_account(self, query: str) -> LocalAccount | None:
        needle = normalize_text(query)
        if not needle:
            return None
        accounts = self.list_accounts(enabled_only=False)
        exact = [
            a for a in accounts
            if a.account_id == query.strip() or normalize_text(a.description) == needle
        ]
        if exact:
            return exact[0]
        contains = [a for a in accounts if needle in normalize_text(a.description)]
        if contains:
            return min(contains, key=lambda a: len(a.description))
        scored = sorted(
            ((SequenceMatcher(None, needle, normalize_text(a.description)).ratio(), a) for a in accounts),
            key=lambda x: x[0],
            reverse=True,
        )
        if scored and scored[0][0] >= 0.72:
            return scored[0][1]
        return None

    def auto_classify_unmapped(self) -> int:
        """Create editable *initial* managerial mappings for categories.

        These are only defaults to make reports useful after the first sync. They
        are persisted in SQLite and never overwrite a user's classification.
        `/classificar` can change any mapping later, so DRE/report logic is not
        permanently tied to these heuristics.
        """
        categories = self.list_categories(active_only=True)
        now = self._utc_now()
        inserted = 0

        def choose(description: str, category_type: int) -> tuple[str, str, str, int, str]:
            n = normalize_text(description)
            # cost_behavior, managerial_group, dre_line, is_opex, note
            if any(x in n for x in ("pro labore", "retirada", "distribuicao de lucro", "socio", "reembolso socio", "adiantamento socio")):
                return ("fixed", "partners", "operating_expenses", 1, "classificação inicial automática")
            if any(x in n for x in ("simples nacional", "imposto", "tribut", "iss", "pis", "cofins", "icms", "irpj", "csll")):
                return ("variable", "taxes", "deductions", 0, "classificação inicial automática")
            if any(x in n for x in ("custo dos produtos", "custo de mercadoria", "mercadoria", "materia prima", "insumo", "instalador", "terceir", "vigilancia e monitoramento tercer", "vigilancia e monitoramento terceir")):
                return ("direct", "operational", "direct_costs", 1, "classificação inicial automática")
            if any(x in n for x in ("marketing", "google ads", "publicidade", "anuncio")):
                return ("variable", "marketing", "operating_expenses", 1, "classificação inicial automática")
            if any(x in n for x in ("software", "contabil", "telefone", "internet", "tarifa bancaria", "taxas pagas", "aluguel", "escritorio", "salario administrativo")):
                return ("fixed", "administrative", "operating_expenses", 1, "classificação inicial automática")
            if any(x in n for x in ("combust", "estacion", "uber", "frete", "ferrament", "manutenc", "servicos de campo", "limpeza e manutenc")):
                return ("variable", "operational", "operating_expenses", 1, "classificação inicial automática")
            if any(x in n for x in ("despesa com emprestimo", "juros pagos", "encargo financeiro")):
                return ("variable", "financial", "other_expenses", 0, "classificação inicial automática")
            if any(x in n for x in ("dividendos", "lucros recebidos", "juros recebidos", "descontos recebidos", "receitas com emprestimos")):
                return ("other", "financial", "other_income", 0, "classificação inicial automática")
            # Bling category types vary by resource/version. We only use it as a
            # weak starting hint; the user can edit the persisted classification.
            if category_type in {1, 3} or any(x in n for x in ("vendas", "receita", "seguranca eletronica", "monitoramento", "servico")):
                return ("other", "revenue", "gross_revenue", 0, "classificação inicial automática")
            return ("other", "other", "auto", 0, "não classificada automaticamente")

        with self._connection() as conn:
            for cat in categories:
                cid = str(cat["category_id"])
                exists = conn.execute(
                    "SELECT 1 FROM category_classifications WHERE category_id=?", (cid,)
                ).fetchone()
                if exists:
                    continue
                values = choose(str(cat["description"]), int(cat.get("category_type") or 0))
                conn.execute(
                    """
                    INSERT INTO category_classifications(
                        category_id,cost_behavior,managerial_group,dre_line,is_opex,notes,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (cid, values[0], values[1], values[2], values[3], values[4], now),
                )
                inserted += 1
            conn.commit()
        return inserted

    def known_suppliers(self, start: date | None = None, end: date | None = None) -> list[str]:
        clauses = ["supplier_name<>''", "status!='E'"]
        params: list[Any] = []
        if start:
            clauses.append("movement_date>=?")
            params.append(start.isoformat())
        if end:
            clauses.append("movement_date<=?")
            params.append(end.isoformat())
        sql = "SELECT supplier_name,COUNT(*) n FROM cash_movements WHERE " + " AND ".join(clauses)
        sql += " GROUP BY lower(supplier_name) ORDER BY n DESC,supplier_name COLLATE NOCASE"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [str(row["supplier_name"]) for row in rows]

    def resolve_supplier(self, query: str, start: date | None = None, end: date | None = None) -> str | None:
        needle = normalize_text(query)
        if not needle:
            return None
        suppliers = self.known_suppliers(start, end)
        exact = [s for s in suppliers if normalize_text(s) == needle]
        if exact:
            return exact[0]
        contains = [s for s in suppliers if needle in normalize_text(s) or normalize_text(s) in needle]
        if contains:
            return min(contains, key=len)
        scored = sorted(
            ((SequenceMatcher(None, needle, normalize_text(s)).ratio(), s) for s in suppliers),
            reverse=True,
        )
        if scored and scored[0][0] >= 0.64:
            return scored[0][1]
        return None

    # ------------------------------------------------------------------
    # Reporting queries
    # ------------------------------------------------------------------
    def movements(
        self,
        *,
        account_id: str | None = None,
        account_query: str | None = None,
        start: date | None = None,
        end: date | None = None,
        direction: str | None = None,
        category_query: str | None = None,
        supplier_query: str | None = None,
        supplier_tax_id: str | None = None,
        text_query: str | None = None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        managerial_group: str | None = None,
        cost_behavior: str | None = None,
        opex_only: bool = False,
        limit: int | None = None,
        enabled_accounts_only: bool = False,
        affects_balance_only: bool = False,
    ) -> tuple[LocalMovement, ...]:
        clauses = ["m.status!='E'"]
        params: list[Any] = []
        if enabled_accounts_only:
            clauses.append("a.enabled=1")
        if account_id is not None:
            clauses.append("m.account_id=?")
            params.append(str(account_id))
        if account_query:
            clauses.append("(lower(a.description) LIKE lower(?) OR m.account_id=?)")
            params.extend([f"%{account_query}%", str(account_query)])
        if start is not None:
            clauses.append("m.movement_date>=?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("m.movement_date<=?")
            params.append(end.isoformat())
        if direction in {"C", "D"}:
            clauses.append("m.direction=?")
            params.append(direction)
        if category_query:
            cat = self.resolve_category(category_query)
            if cat:
                ids = sorted(self.category_descendants(str(cat["category_id"])))
                placeholders = ",".join("?" for _ in ids)
                clauses.append(f"m.category_id IN ({placeholders})")
                params.extend(ids)
            else:
                clauses.append("lower(COALESCE(NULLIF(c.description,''),m.category_name)) LIKE lower(?)")
                params.append(f"%{category_query}%")
        if supplier_query:
            clauses.append("lower(m.supplier_name) LIKE lower(?)")
            params.append(f"%{supplier_query}%")
        if supplier_tax_id:
            digits = re.sub(r"\D", "", supplier_tax_id)
            clauses.append("replace(replace(replace(replace(m.supplier_tax_id,'.',''),'/',''),'-',''),' ','') LIKE ?")
            params.append(f"%{digits}%")
        if text_query:
            token = f"%{text_query}%"
            clauses.append("(lower(m.supplier_name) LIKE lower(?) OR lower(m.description) LIKE lower(?) OR lower(COALESCE(NULLIF(c.description,''),m.category_name)) LIKE lower(?))")
            params.extend([token, token, token])
        if min_amount is not None:
            clauses.append("m.amount_cents>=?")
            params.append(_to_cents(min_amount))
        if max_amount is not None:
            clauses.append("m.amount_cents<=?")
            params.append(_to_cents(max_amount))
        if managerial_group:
            clauses.append("COALESCE(cc.managerial_group,'other')=?")
            params.append(managerial_group)
        if cost_behavior:
            clauses.append("COALESCE(cc.cost_behavior,'other')=?")
            params.append(cost_behavior)
        if opex_only:
            clauses.append("COALESCE(cc.is_opex,0)=1")
        if affects_balance_only:
            clauses.append("m.affects_balance=1")

        sql = """
            SELECT m.*,
                   COALESCE(NULLIF(c.description,''),m.category_name,'') resolved_category
              FROM cash_movements m
              JOIN cash_accounts a ON a.account_id=m.account_id
              LEFT JOIN categories c ON c.category_id=m.category_id
              LEFT JOIN category_classifications cc ON cc.category_id=m.category_id
        """
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY m.movement_date DESC,m.movement_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return tuple(
            LocalMovement(
                movement_id=str(row["movement_id"]),
                account_id=str(row["account_id"]),
                movement_date=date.fromisoformat(row["movement_date"]),
                direction=str(row["direction"]),
                amount=Decimal(int(row["amount_cents"])) / Decimal(100),
                category_id=str(row["category_id"]) if row["category_id"] not in (None, "") else None,
                category_name=str(row["resolved_category"] or "Sem categoria"),
                supplier_name=str(row["supplier_name"] or ""),
                supplier_tax_id=str(row["supplier_tax_id"] or ""),
                description=str(row["description"] or ""),
                affects_balance=bool(row["affects_balance"]),
                status=str(row["status"] or "R"),
            )
            for row in rows
        )

    def aggregate_by_category(
        self,
        start: date,
        end: date,
        *,
        direction: str = "D",
        category_query: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        movements = self.movements(
            start=start, end=end, direction=direction, category_query=category_query, account_id=account_id
        )
        buckets: dict[tuple[str | None, str], dict[str, Any]] = {}
        for m in movements:
            key = (m.category_id, m.category_name or "Sem categoria")
            item = buckets.setdefault(key, {"category_id": m.category_id, "category": key[1], "total": Decimal("0"), "count": 0})
            item["total"] += m.amount
            item["count"] += 1
        rows = list(buckets.values())
        rows.sort(key=lambda x: x["total"], reverse=True)
        return rows

    def aggregate_by_supplier(
        self,
        start: date,
        end: date,
        *,
        direction: str = "D",
        supplier_query: str | None = None,
    ) -> list[dict[str, Any]]:
        movements = self.movements(start=start, end=end, direction=direction, supplier_query=supplier_query)
        buckets: dict[str, dict[str, Any]] = {}
        for m in movements:
            name = m.supplier_name.strip() or "Sem fornecedor identificado"
            key = normalize_text(name) or name
            item = buckets.setdefault(key, {"supplier": name, "tax_id": m.supplier_tax_id, "total": Decimal("0"), "count": 0, "first": m.movement_date, "last": m.movement_date})
            item["total"] += m.amount
            item["count"] += 1
            item["first"] = min(item["first"], m.movement_date)
            item["last"] = max(item["last"], m.movement_date)
            if not item["tax_id"] and m.supplier_tax_id:
                item["tax_id"] = m.supplier_tax_id
        rows = list(buckets.values())
        rows.sort(key=lambda x: x["total"], reverse=True)
        return rows

    def classification_rows(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT c.category_id,c.description,c.category_type,c.parent_id,
                       COALESCE(cc.cost_behavior,'other') cost_behavior,
                       COALESCE(cc.managerial_group,'other') managerial_group,
                       COALESCE(cc.dre_line,'auto') dre_line,
                       COALESCE(cc.is_opex,0) is_opex
                  FROM categories c
                  LEFT JOIN category_classifications cc ON cc.category_id=c.category_id
                 WHERE c.active=1
                 ORDER BY c.description COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]


    def coverage_gaps(self, start: date, end: date) -> list[tuple[date, date]]:
        """Return unsynchronized gaps inside a requested period."""
        if end < start:
            return [(start, end)]
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT start_date,end_date FROM sync_periods
                 WHERE end_date>=? AND start_date<=?
                 ORDER BY start_date,end_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        intervals: list[tuple[date, date]] = []
        for row in rows:
            a = max(start, date.fromisoformat(row["start_date"]))
            b = min(end, date.fromisoformat(row["end_date"]))
            if b < a:
                continue
            if not intervals or a > intervals[-1][1] + timedelta(days=1):
                intervals.append((a, b))
            else:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], b))
        gaps: list[tuple[date, date]] = []
        cursor = start
        for a, b in intervals:
            if a > cursor:
                gaps.append((cursor, a - timedelta(days=1)))
            cursor = max(cursor, b + timedelta(days=1))
        if cursor <= end:
            gaps.append((cursor, end))
        return gaps

    def synced_periods(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM sync_periods ORDER BY synced_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> SyncStatus:
        with self._connection() as conn:
            counts = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM cash_movements) movements,
                    (SELECT COUNT(*) FROM cash_accounts) accounts,
                    (SELECT COUNT(*) FROM cash_accounts WHERE enabled=1) enabled_accounts,
                    (SELECT COUNT(*) FROM cash_accounts WHERE enabled=1 AND base_date IS NOT NULL) calibrated_accounts,
                    (SELECT COUNT(*) FROM categories) categories,
                    (SELECT MIN(movement_date) FROM cash_movements) oldest_date,
                    (SELECT MAX(movement_date) FROM cash_movements) newest_date
                """
            ).fetchone()
            state_rows = conn.execute("SELECT key,value FROM sync_state").fetchall()
        state = {str(row["key"]): str(row["value"]) for row in state_rows}
        return SyncStatus(
            movements=int(counts["movements"]),
            accounts=int(counts["accounts"]),
            enabled_accounts=int(counts["enabled_accounts"]),
            calibrated_accounts=int(counts["calibrated_accounts"]),
            categories=int(counts["categories"]),
            oldest_date=_parse_date(counts["oldest_date"]),
            newest_date=_parse_date(counts["newest_date"]),
            last_sync_at=_parse_datetime(state.get("last_sync_at")),
            last_sync_start=_parse_date(state.get("last_sync_start")),
            last_sync_end=_parse_date(state.get("last_sync_end")),
            last_category_sync_at=_parse_datetime(state.get("last_category_sync_at")),
        )
