from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, month_keys, safe_pct


def run(
    db: FinanceDB,
    period: Period,
    *,
    filters: ReportFilters | None = None,
) -> ReportResult:
    """Generic expense lookup used for natural-language subjects.

    This covers questions such as "quanto gastei com OpenAI" even when the
    searched text appears only in the movement history/description instead of
    being a clean supplier or category in Bling.
    """
    f = filters or ReportFilters()
    rows = fetch_movements(db, period, direction="D", filters=f)
    total = sum((m.amount for m in rows), Decimal("0"))
    all_expenses = sum(
        (m.amount for m in db.movements(start=period.start, end=period.end, direction="D")),
        Decimal("0"),
    )
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    suppliers: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in rows:
        categories[m.category_name or "Sem categoria"] += m.amount
        suppliers[m.supplier_name.strip() or "Sem fornecedor"] += m.amount
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"] += m.amount
    months = max(1, len(month_keys(period.start, period.end)))
    subject = f.text_query or f.supplier or f.category or "filtro informado"
    return ReportResult(
        "expense_search",
        f"Despesas — {subject}",
        period,
        {
            "subject": subject,
            "total": total,
            "count": len(rows),
            "ticket_avg": total / len(rows) if rows else Decimal("0"),
            "monthly_avg": total / months,
            "expense_share": safe_pct(total, all_expenses),
            "categories": sorted(categories.items(), key=lambda x: x[1], reverse=True),
            "suppliers": sorted(suppliers.items(), key=lambda x: x[1], reverse=True),
            "monthly": sorted(monthly.items()),
        },
        coverage_warnings(db, period),
    )
