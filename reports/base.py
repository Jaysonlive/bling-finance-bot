from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finance_db import FinanceDB, LocalMovement
from .common import Period, format_period


@dataclass(slots=True)
class ReportFilters:
    category: str | None = None
    supplier: str | None = None
    supplier_tax_id: str | None = None
    account_id: str | None = None
    text_query: str | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None
    direction: str | None = None


@dataclass(slots=True)
class ReportResult:
    report_type: str
    title: str
    period: Period
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def coverage_warnings(db: FinanceDB, period: Period) -> list[str]:
    gaps = db.coverage_gaps(period.start, period.end)
    if not gaps:
        return []
    pieces = [format_period(a, b) for a, b in gaps[:3]]
    extra = "" if len(gaps) <= 3 else f" e mais {len(gaps)-3} intervalo(s)"
    return [
        "⚠️ O banco local não tem sincronização completa para: "
        + "; ".join(pieces)
        + extra
        + ". Use /sincronizar INICIO FIM para trazer esse histórico do Bling."
    ]


def fetch_movements(
    db: FinanceDB,
    period: Period,
    *,
    direction: str | None = None,
    filters: ReportFilters | None = None,
    managerial_group: str | None = None,
    cost_behavior: str | None = None,
    opex_only: bool = False,
) -> tuple[LocalMovement, ...]:
    f = filters or ReportFilters()
    return db.movements(
        start=period.start,
        end=period.end,
        direction=direction or f.direction,
        category_query=f.category,
        supplier_query=f.supplier,
        supplier_tax_id=f.supplier_tax_id,
        account_id=f.account_id,
        text_query=f.text_query,
        min_amount=f.min_amount,
        max_amount=f.max_amount,
        managerial_group=managerial_group,
        cost_behavior=cost_behavior,
        opex_only=opex_only,
    )


def total_amount(rows: list[dict[str, Any]]) -> Decimal:
    return sum((Decimal(str(row.get("total", 0))) for row in rows), Decimal("0"))
