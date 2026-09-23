from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB, normalize_text
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys, safe_pct

KEYWORDS = ("pro labore", "retirada", "distribuicao de lucro", "adiantamento", "reembolso", "socio")


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None) -> ReportResult:
    rows = fetch_movements(db, period, direction="D", filters=filters)
    selected = []
    for m in rows:
        n = normalize_text(f"{m.category_name} {m.description} {m.supplier_name}")
        if any(k in n for k in KEYWORDS):
            selected.append(m)
            continue
        # User-managed classification takes precedence when present.
        cat = db.resolve_category(m.category_name)
        if cat:
            cls = next((r for r in db.classification_rows() if str(r["category_id"]) == str(cat["category_id"])), None)
            if cls and cls.get("managerial_group") == "partners":
                selected.append(m)

    people: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    yearly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in selected:
        people[m.supplier_name.strip() or "Não identificado"] += m.amount
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"] += m.amount
        yearly[str(m.movement_date.year)] += m.amount
    total = sum((m.amount for m in selected), Decimal("0"))
    months = max(1, len(month_keys(period.start, period.end)))
    revenue = sum((m.amount for m in fetch_movements(db, period, direction="C")), Decimal("0"))
    return ReportResult(
        "partners",
        "Pró-labore, retiradas e sócios",
        period,
        {
            "total": total,
            "monthly_avg": total / months,
            "people": sorted(people.items(), key=lambda x: x[1], reverse=True),
            "monthly": sorted(monthly.items()),
            "yearly": sorted(yearly.items()),
            "revenue_share": safe_pct(total, revenue),
            "count": len(selected),
        },
        coverage_warnings(db, period),
    )
