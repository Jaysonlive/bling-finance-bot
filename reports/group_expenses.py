from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys, safe_pct


def run_group(
    db: FinanceDB,
    period: Period,
    *,
    managerial_group: str,
    title: str,
    filters: ReportFilters | None = None,
) -> ReportResult:
    movements = fetch_movements(db, period, direction="D", filters=filters, managerial_group=managerial_group)
    total = sum((m.amount for m in movements), Decimal("0"))
    months = max(1, len(month_keys(period.start, period.end)))
    total_expenses = sum((m.amount for m in fetch_movements(db, period, direction="D", filters=filters)), Decimal("0"))
    revenue = sum((m.amount for m in fetch_movements(db, period, direction="C", filters=filters)), Decimal("0"))
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in movements:
        categories[m.category_name] += m.amount
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"] += m.amount
    return ReportResult(
        managerial_group,
        title,
        period,
        {
            "total": total,
            "monthly_avg": total / months,
            "expense_share": safe_pct(total, total_expenses),
            "revenue_share": safe_pct(total, revenue),
            "categories": sorted(categories.items(), key=lambda x: x[1], reverse=True),
            "monthly": sorted(monthly.items()),
            "count": len(movements),
        },
        coverage_warnings(db, period),
    )
