from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, safe_pct


def run(
    db: FinanceDB,
    period: Period,
    *,
    threshold: Decimal | None = None,
    filters: ReportFilters | None = None,
) -> ReportResult:
    threshold = threshold or Decimal(db.get_setting("small_expense_threshold", "100") or "100")
    f = filters or ReportFilters()
    effective_max = threshold if f.max_amount is None else min(threshold, f.max_amount)
    f = ReportFilters(
        category=f.category,
        supplier=f.supplier,
        supplier_tax_id=f.supplier_tax_id,
        account_id=f.account_id,
        text_query=f.text_query,
        min_amount=f.min_amount,
        max_amount=effective_max,
    )
    rows = fetch_movements(db, period, direction="D", filters=f)
    total = sum((m.amount for m in rows), Decimal("0"))
    all_expenses = sum(
        (m.amount for m in fetch_movements(db, period, direction="D", filters=filters)),
        Decimal("0"),
    )
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    frequent: Counter[str] = Counter()
    for m in rows:
        categories[m.category_name or "Sem categoria"] += m.amount
        label = m.supplier_name.strip() or m.description.strip() or "Não identificado"
        frequent[label] += 1
    return ReportResult(
        "small_expenses",
        f"Pequenas despesas até R$ {threshold}",
        period,
        {
            "threshold": threshold,
            "count": len(rows),
            "total": total,
            "ticket_avg": total / len(rows) if rows else Decimal("0"),
            "expense_share": safe_pct(total, all_expenses),
            "categories": sorted(categories.items(), key=lambda x: x[1], reverse=True),
            "frequent": frequent.most_common(10),
        },
        coverage_warnings(db, period),
    )
