from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, safe_pct


def _supplier_rows(db: FinanceDB, period: Period, filters: ReportFilters | None = None) -> list[dict]:
    movements = fetch_movements(db, period, direction="D", filters=filters)
    buckets: dict[str, dict] = {}
    for m in movements:
        name = m.supplier_name.strip() or "Sem fornecedor identificado"
        key = name.casefold()
        item = buckets.setdefault(
            key,
            {
                "supplier": name,
                "tax_id": m.supplier_tax_id,
                "total": Decimal("0"),
                "count": 0,
                "first": m.movement_date,
                "last": m.movement_date,
            },
        )
        item["total"] += m.amount
        item["count"] += 1
        item["first"] = min(item["first"], m.movement_date)
        item["last"] = max(item["last"], m.movement_date)
        if not item["tax_id"] and m.supplier_tax_id:
            item["tax_id"] = m.supplier_tax_id
    rows = list(buckets.values())
    rows.sort(key=lambda x: x["total"], reverse=True)
    return rows


def ranking(
    db: FinanceDB,
    period: Period,
    *,
    top_n: int = 10,
    filters: ReportFilters | None = None,
) -> ReportResult:
    rows = _supplier_rows(db, period, filters)
    total = sum((r["total"] for r in rows), Decimal("0"))
    cumulative = Decimal("0")
    pareto_count = 0
    for row in rows:
        row["percent"] = safe_pct(row["total"], total)
        row["ticket_avg"] = row["total"] / row["count"] if row["count"] else Decimal("0")
        if total and cumulative / total < Decimal("0.80"):
            cumulative += row["total"]
            pareto_count += 1
    top5 = sum((r["total"] for r in rows[:5]), Decimal("0"))
    return ReportResult(
        report_type="supplier_ranking",
        title=f"Top {top_n} fornecedores",
        period=period,
        data={
            "total": total,
            "rows": rows[:top_n],
            "top5_percent": safe_pct(top5, total),
            "pareto_80_count": pareto_count,
            "all_rows": rows,
        },
        warnings=coverage_warnings(db, period),
    )


def history(
    db: FinanceDB,
    period: Period,
    supplier: str,
    *,
    filters: ReportFilters | None = None,
) -> ReportResult:
    resolved = db.resolve_supplier(supplier, period.start, period.end) or supplier
    f = filters or ReportFilters()
    f = ReportFilters(
        category=f.category,
        supplier=resolved,
        supplier_tax_id=f.supplier_tax_id,
        account_id=f.account_id,
        text_query=f.text_query,
        min_amount=f.min_amount,
        max_amount=f.max_amount,
    )
    movements = fetch_movements(db, period, direction="D", filters=f)
    total = sum((m.amount for m in movements), Decimal("0"))
    categories: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    yearly: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for m in movements:
        categories[m.category_name or "Sem categoria"] += m.amount
        monthly[f"{m.movement_date.year:04d}-{m.movement_date.month:02d}"] += m.amount
        yearly[str(m.movement_date.year)] += m.amount
    months_with_data = len(monthly)
    total_expenses = sum(
        (m.amount for m in db.movements(start=period.start, end=period.end, direction="D")),
        Decimal("0"),
    )
    return ReportResult(
        report_type="supplier_history",
        title=f"Histórico — {resolved}",
        period=period,
        data={
            "supplier": resolved,
            "total": total,
            "count": len(movements),
            "ticket_avg": total / len(movements) if movements else Decimal("0"),
            "monthly_avg": total / months_with_data if months_with_data else Decimal("0"),
            "first": min((m.movement_date for m in movements), default=None),
            "last": max((m.movement_date for m in movements), default=None),
            "categories": sorted(categories.items(), key=lambda x: x[1], reverse=True),
            "monthly": sorted(monthly.items()),
            "yearly": sorted(yearly.items()),
            "expense_share": safe_pct(total, total_expenses),
        },
        warnings=coverage_warnings(db, period),
    )
