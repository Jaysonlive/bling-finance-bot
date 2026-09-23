from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, safe_pct, month_keys


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None) -> ReportResult:
    labels = {
        "fixed": "Fixas",
        "variable": "Variáveis",
        "direct": "Custos diretos",
        "administrative": "Administrativas",
        "other": "Outras / não classificadas",
    }
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    counts: dict[str, int] = defaultdict(int)
    for key in labels:
        rows = fetch_movements(db, period, direction="D", filters=filters, cost_behavior=key)
        totals[key] = sum((m.amount for m in rows), Decimal("0"))
        counts[key] = len(rows)
    total = sum(totals.values(), Decimal("0"))
    months = max(1, len(month_keys(period.start, period.end)))
    rows = [
        {
            "key": key,
            "label": label,
            "total": totals[key],
            "count": counts[key],
            "percent": safe_pct(totals[key], total),
            "monthly_avg": totals[key] / months,
        }
        for key, label in labels.items()
    ]
    return ReportResult(
        "fixed_variable",
        "Despesas fixas x variáveis",
        period,
        {"total": total, "rows": rows},
        coverage_warnings(db, period),
    )
