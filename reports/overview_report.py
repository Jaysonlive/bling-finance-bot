from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys, previous_equivalent, safe_pct


def _category_totals(db: FinanceDB, period: Period, filters: ReportFilters | None) -> dict[str, Decimal]:
    buckets: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in fetch_movements(db, period, direction="D", filters=filters):
        buckets[m.category_name or "Sem categoria"] += m.amount
    return buckets


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None) -> ReportResult:
    credits = fetch_movements(db, period, direction="C", filters=filters)
    debits = fetch_movements(db, period, direction="D", filters=filters)
    revenue = sum((m.amount for m in credits), Decimal("0"))
    expenses = sum((m.amount for m in debits), Decimal("0"))
    result = revenue - expenses
    months = max(1, len(month_keys(period.start, period.end)))
    cats: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    suppliers: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"revenue": Decimal("0"), "expenses": Decimal("0")})
    for m in credits:
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"]["revenue"] += m.amount
    for m in debits:
        cats[m.category_name or "Sem categoria"] += m.amount
        suppliers[m.supplier_name.strip() or "Sem fornecedor"] += m.amount
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"]["expenses"] += m.amount

    administrative = sum((m.amount for m in fetch_movements(db, period, direction="D", filters=filters, managerial_group="administrative")), Decimal("0"))
    operational = sum((m.amount for m in fetch_movements(db, period, direction="D", filters=filters, managerial_group="operational")), Decimal("0"))
    opex = sum((m.amount for m in fetch_movements(db, period, direction="D", filters=filters, opex_only=True)), Decimal("0"))
    threshold = Decimal(db.get_setting("small_expense_threshold", "100") or "100")
    small = [m for m in debits if m.amount <= threshold]
    small_total = sum((m.amount for m in small), Decimal("0"))

    previous = previous_equivalent(period)
    previous_debits = fetch_movements(db, previous, direction="D", filters=filters)
    previous_expenses = sum((m.amount for m in previous_debits), Decimal("0"))
    expense_difference = expenses - previous_expenses
    expense_change = (expense_difference / previous_expenses * Decimal("100")) if previous_expenses else Decimal("0")
    prev_cats = _category_totals(db, previous, filters)
    increases = []
    for name, current_value in cats.items():
        prev = prev_cats.get(name, Decimal("0"))
        diff = current_value - prev
        if diff <= 0:
            continue
        variation = (diff / prev * Decimal("100")) if prev else Decimal("100")
        increases.append((name, current_value, prev, diff, variation))
    increases.sort(key=lambda x: x[3], reverse=True)

    return ReportResult(
        "overview",
        "Resumo financeiro",
        period,
        {
            "revenue": revenue,
            "expenses": expenses,
            "result": result,
            "margin": safe_pct(result, revenue),
            "monthly_revenue_avg": revenue / months,
            "monthly_expense_avg": expenses / months,
            "top_categories": sorted(cats.items(), key=lambda x: x[1], reverse=True)[:5],
            "top_suppliers": sorted(suppliers.items(), key=lambda x: x[1], reverse=True)[:5],
            "monthly": sorted(monthly.items()),
            "count_credits": len(credits),
            "count_debits": len(debits),
            "administrative": administrative,
            "operational": operational,
            "opex": opex,
            "small_total": small_total,
            "small_count": len(small),
            "small_threshold": threshold,
            "previous_period": previous,
            "previous_expenses": previous_expenses,
            "expense_difference": expense_difference,
            "expense_change_pct": expense_change,
            "top_increases": increases[:5],
        },
        coverage_warnings(db, period) + coverage_warnings(db, previous),
    )
