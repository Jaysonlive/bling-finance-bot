from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from finance_db import FinanceDB
from .base import ReportFilters, ReportResult, coverage_warnings, fetch_movements
from .common import Period, safe_pct


LINES = {
    "gross_revenue": "Receita Bruta",
    "deductions": "Deduções / Impostos",
    "direct_costs": "Custos diretos",
    "operating_expenses": "Despesas Operacionais",
    "other_income": "Outras receitas",
    "other_expenses": "Outras despesas",
    "ignore": "Ignorar",
}


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None) -> ReportResult:
    classifications = {str(r["category_id"]): r for r in db.classification_rows()}
    movements = fetch_movements(db, period, filters=filters)
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    details: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: Decimal("0")))
    auto_categories: set[str] = set()

    for m in movements:
        cls = classifications.get(str(m.category_id or ""))
        dre_line = str(cls.get("dre_line") if cls else "auto")
        if dre_line == "auto":
            # Safe cash-basis fallback so the report remains complete, while
            # explicitly warning the user to classify the categories.
            dre_line = "gross_revenue" if m.direction == "C" else "operating_expenses"
            auto_categories.add(m.category_name or "Sem categoria")
        if dre_line == "ignore":
            continue
        if dre_line in {"gross_revenue", "other_income"} and m.direction != "C":
            # A debit mapped to an income line reduces that line (refund/estorno).
            amount = -m.amount
        elif dre_line in {"deductions", "direct_costs", "operating_expenses", "other_expenses"} and m.direction == "C":
            amount = -m.amount
        else:
            amount = m.amount
        totals[dre_line] += amount
        details[dre_line][m.category_name or "Sem categoria"] += amount

    gross = totals["gross_revenue"]
    deductions = totals["deductions"]
    net_revenue = gross - deductions
    direct = totals["direct_costs"]
    gross_profit = net_revenue - direct
    opex = totals["operating_expenses"]
    operating_result = gross_profit - opex
    other_income = totals["other_income"]
    other_expenses = totals["other_expenses"]
    net_result = operating_result + other_income - other_expenses

    data = {
        "gross_revenue": gross,
        "deductions": deductions,
        "net_revenue": net_revenue,
        "direct_costs": direct,
        "gross_profit": gross_profit,
        "operating_expenses": opex,
        "operating_result": operating_result,
        "other_income": other_income,
        "other_expenses": other_expenses,
        "net_result": net_result,
        "gross_margin": safe_pct(gross_profit, gross),
        "operating_margin": safe_pct(operating_result, gross),
        "net_margin": safe_pct(net_result, gross),
        "details": {k: sorted(v.items(), key=lambda x: abs(x[1]), reverse=True) for k, v in details.items()},
        "auto_categories": sorted(auto_categories),
    }
    warnings = coverage_warnings(db, period)
    if auto_categories:
        warnings.append(
            f"⚠️ {len(auto_categories)} categoria(s) ainda usam mapeamento DRE automático. "
            "Revise com /categorias e /classificar para uma DRE gerencial mais precisa."
        )
    return ReportResult("dre", "DRE gerencial (base caixa)", period, data, warnings)
