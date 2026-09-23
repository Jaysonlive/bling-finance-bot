from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, add_months, month_end


def run(
    db: FinanceDB,
    period: Period,
    *,
    baseline_months: int = 6,
    filters: ReportFilters | None = None,
) -> ReportResult:
    pct_threshold = Decimal(db.get_setting("anomaly_percent_threshold", "30") or "30")
    min_amount = Decimal(db.get_setting("anomaly_min_amount", "100") or "100")
    current = fetch_movements(db, period, direction="D", filters=filters)
    cur: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in current:
        cur[m.category_name or "Sem categoria"] += m.amount

    baseline_end = period.start - timedelta(days=1)
    baseline_start_month = add_months(date(period.start.year, period.start.month, 1), -baseline_months)
    baseline = Period(baseline_start_month, baseline_end, f"{baseline_months} meses anteriores")
    historical = fetch_movements(db, baseline, direction="D", filters=filters)
    monthly: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for m in historical:
        monthly[(m.category_name or "Sem categoria", f"{m.movement_date.year:04d}-{m.movement_date.month:02d}")] += m.amount

    rows = []
    categories = set(cur) | {k[0] for k in monthly}
    for category in categories:
        month_values = [value for (cat, _), value in monthly.items() if cat == category]
        if not month_values:
            continue
        avg = sum(month_values, Decimal("0")) / Decimal(baseline_months)
        current_value = cur.get(category, Decimal("0"))
        diff = current_value - avg
        variation = (diff / avg * Decimal("100")) if avg else Decimal("0")
        significant = current_value >= min_amount and variation >= pct_threshold
        if significant:
            rows.append(
                {
                    "category": category,
                    "current": current_value,
                    "baseline_avg": avg,
                    "difference": diff,
                    "variation_pct": variation,
                }
            )
    rows.sort(key=lambda x: x["variation_pct"], reverse=True)
    warnings = coverage_warnings(db, period) + coverage_warnings(db, baseline)
    return ReportResult(
        "anomalies",
        "Gastos fora do padrão",
        period,
        {
            "rows": rows,
            "baseline_months": baseline_months,
            "pct_threshold": pct_threshold,
            "min_amount": min_amount,
            "baseline_period": baseline,
        },
        warnings,
    )
