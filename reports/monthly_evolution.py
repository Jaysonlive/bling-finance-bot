from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys


def run(
    db: FinanceDB,
    period: Period,
    *,
    category: str | None = None,
    supplier: str | None = None,
    filters: ReportFilters | None = None,
) -> ReportResult:
    f = filters or ReportFilters()
    f = ReportFilters(
        category=category or f.category,
        supplier=supplier or f.supplier,
        supplier_tax_id=f.supplier_tax_id,
        account_id=f.account_id,
        text_query=f.text_query,
        min_amount=f.min_amount,
        max_amount=f.max_amount,
    )
    movements = fetch_movements(db, period, direction="D", filters=f)
    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in movements:
        buckets[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"] += m.amount
    rows = []
    for year, month in month_keys(period.start, period.end):
        key = f"{year:04d}-{month:02d}"
        rows.append({"month": key, "total": buckets[key]})
    values = [r["total"] for r in rows]
    total = sum(values, Decimal("0"))
    average = total / len(rows) if rows else Decimal("0")
    max_row = max(rows, key=lambda x: x["total"], default=None)
    min_row = min(rows, key=lambda x: x["total"], default=None)
    for idx, row in enumerate(rows):
        prev = rows[idx - 1]["total"] if idx else Decimal("0")
        row["variation_pct"] = ((row["total"] - prev) / prev * Decimal("100")) if prev else Decimal("0")
    trend = "estável"
    if len(rows) >= 4:
        half = max(1, len(rows) // 2)
        first_avg = sum((r["total"] for r in rows[:half]), Decimal("0")) / half
        second_items = rows[half:]
        second_avg = sum((r["total"] for r in second_items), Decimal("0")) / max(1, len(second_items))
        if second_avg > first_avg * Decimal("1.10"):
            trend = "alta"
        elif second_avg < first_avg * Decimal("0.90"):
            trend = "queda"
    return ReportResult(
        "monthly_evolution",
        "Evolução mensal das despesas",
        period,
        {
            "rows": rows,
            "total": total,
            "average": average,
            "max": max_row,
            "min": min_row,
            "trend": trend,
            "category": f.category,
            "supplier": f.supplier,
        },
        coverage_warnings(db, period),
    )
