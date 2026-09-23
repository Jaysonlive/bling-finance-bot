from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from statistics import median

from finance_db import FinanceDB, normalize_text
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period


def _signature(supplier: str, description: str) -> str:
    # Remove long IDs, dates and invoice numbers so recurring descriptions such
    # as "OPENAI 08/2026" and "OPENAI 09/2026" group together.
    text = normalize_text(f"{supplier} {description}")
    text = re.sub(r"\b\d{2,}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:160]


def run(
    db: FinanceDB,
    period: Period,
    *,
    filters: ReportFilters | None = None,
) -> ReportResult:
    movements = fetch_movements(db, period, direction="D", filters=filters)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for m in movements:
        supplier = m.supplier_name.strip() or "Sem fornecedor"
        sig = _signature(supplier, m.description)
        if not sig:
            continue
        groups[(normalize_text(supplier), sig)].append(m)

    rows = []
    for (_supplier_key, _sig), items in groups.items():
        if len(items) < 3:
            continue
        items = sorted(items, key=lambda x: x.movement_date)
        dates = [m.movement_date for m in items]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if b > a]
        if not gaps:
            continue
        gap = float(median(gaps))
        months = len({(d.year, d.month) for d in dates})
        # Require temporal repetition, not merely duplicate values.
        if months < 2 and gap > 14:
            continue
        amounts = [m.amount for m in items]
        avg = sum(amounts, Decimal("0")) / len(amounts)
        if avg <= 0:
            continue
        dispersion = (max(amounts) - min(amounts)) / avg if avg else Decimal("0")
        if gap <= 10:
            frequency = "semanal"
            monthly_factor = Decimal("4.345")
        elif gap <= 45:
            frequency = "mensal"
            monthly_factor = Decimal("1")
        elif gap <= 120:
            frequency = "trimestral"
            monthly_factor = Decimal("0.333333")
        elif gap <= 410:
            frequency = "anual"
            monthly_factor = Decimal("0.083333")
        else:
            continue
        # A high dispersion is acceptable only when the textual signature and
        # supplier are stable; flag confidence lower instead of dropping it.
        confidence = "alta" if dispersion <= Decimal("0.20") else "média"
        rows.append(
            {
                "supplier": items[-1].supplier_name or "Sem fornecedor",
                "description": items[-1].description or items[-1].category_name,
                "category": items[-1].category_name,
                "average": avg,
                "count": len(items),
                "frequency": frequency,
                "last": items[-1].movement_date,
                "monthly_estimate": avg * monthly_factor,
                "annual_estimate": avg * monthly_factor * Decimal("12"),
                "confidence": confidence,
            }
        )
    rows.sort(key=lambda x: x["annual_estimate"], reverse=True)
    return ReportResult(
        "recurring",
        "Despesas recorrentes / assinaturas",
        period,
        {"rows": rows, "monthly_estimate": sum((r["monthly_estimate"] for r in rows), Decimal("0"))},
        coverage_warnings(db, period),
    )
