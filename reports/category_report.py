from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, safe_pct


def _rows(db: FinanceDB, period: Period, filters: ReportFilters | None = None) -> list[dict]:
    movements = fetch_movements(db, period, direction="D", filters=filters)
    buckets: dict[tuple[str | None, str], dict] = {}
    for m in movements:
        key = (m.category_id, m.category_name or "Sem categoria")
        item = buckets.setdefault(
            key,
            {"category_id": m.category_id, "category": key[1], "total": Decimal("0"), "count": 0},
        )
        item["total"] += m.amount
        item["count"] += 1
    rows = list(buckets.values())
    rows.sort(key=lambda x: x["total"], reverse=True)
    total = sum((r["total"] for r in rows), Decimal("0"))
    for row in rows:
        row["percent"] = safe_pct(row["total"], total)
        row["ticket_avg"] = row["total"] / row["count"] if row["count"] else Decimal("0")
        # "Cada R$ 100" is numerically the percentage expressed in reais.
        row["per_100"] = safe_pct(row["total"], total)
    return rows


def distribution(
    db: FinanceDB,
    period: Period,
    *,
    top_n: int | None = None,
    filters: ReportFilters | None = None,
) -> ReportResult:
    rows = _rows(db, period, filters)
    total = sum((row["total"] for row in rows), Decimal("0"))
    shown = rows if top_n is None else rows[:top_n]
    return ReportResult(
        report_type="category_distribution",
        title="Para onde vão cada R$ 100 gastos",
        period=period,
        data={"total": total, "rows": shown, "all_rows": rows},
        warnings=coverage_warnings(db, period),
    )


def ranking(
    db: FinanceDB,
    period: Period,
    *,
    top_n: int = 10,
    filters: ReportFilters | None = None,
) -> ReportResult:
    result = distribution(db, period, top_n=top_n, filters=filters)
    result.report_type = "category_ranking"
    result.title = f"Top {top_n} categorias de despesas"
    return result


def compare(
    db: FinanceDB,
    current: Period,
    previous: Period,
    *,
    filters: ReportFilters | None = None,
) -> ReportResult:
    a = {r["category"]: r for r in _rows(db, current, filters)}
    b = {r["category"]: r for r in _rows(db, previous, filters)}
    names = set(a) | set(b)
    rows = []
    for name in names:
        current_total = a.get(name, {}).get("total", Decimal("0"))
        previous_total = b.get(name, {}).get("total", Decimal("0"))
        diff = current_total - previous_total
        pct = (diff / previous_total * Decimal("100")) if previous_total else (Decimal("100") if current_total else Decimal("0"))
        rows.append(
            {
                "category": name,
                "current": current_total,
                "previous": previous_total,
                "difference": diff,
                "difference_pct": pct,
            }
        )
    rows.sort(key=lambda x: abs(x["difference"]), reverse=True)
    return ReportResult(
        "category_variation",
        "Variação por categoria",
        current,
        {"rows": rows, "previous_period": previous},
        coverage_warnings(db, current) + coverage_warnings(db, previous),
    )
