from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys, safe_pct


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None) -> ReportResult:
    movements = fetch_movements(db, period, direction="D", filters=filters, opex_only=True)
    total = sum((m.amount for m in movements), Decimal("0"))
    months = max(1, len(month_keys(period.start, period.end)))
    monthly_avg = total / months
    workdays = Decimal(db.get_setting("workdays_per_month", "21") or "21")
    hours = Decimal(db.get_setting("workhours_per_day", "8") or "8")
    per_day = monthly_avg / workdays if workdays else Decimal("0")
    per_hour = per_day / hours if hours else Decimal("0")
    revenue = sum((m.amount for m in fetch_movements(db, period, direction="C", filters=filters)), Decimal("0"))
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in movements:
        categories[m.category_name] += m.amount
    return ReportResult(
        "opex",
        "Custo operacional / OPEX",
        period,
        {
            "total": total,
            "monthly_avg": monthly_avg,
            "workdays": workdays,
            "hours_per_day": hours,
            "per_day": per_day,
            "per_hour": per_hour,
            "revenue_share": safe_pct(total, revenue),
            "categories": sorted(categories.items(), key=lambda x: x[1], reverse=True),
            "count": len(movements),
        },
        coverage_warnings(db, period),
    )
