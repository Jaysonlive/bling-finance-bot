from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable

from finance_db import FinanceDB, normalize_text
from reports.base import ReportFilters, ReportResult
from reports.common import Period, MONTHS, month_end, parse_period, parse_two_years, previous_equivalent, format_period
from reports.formatting import brl, pct, signed_brl, signed_pct
from reports import category_report, supplier_report, fixed_variable_report, monthly_evolution
from .ai_interpreter import AIIntentInterpreter
from reports import recurring_report, partner_report, administrative_expenses, operational_expenses
from reports import dre_report, opex_report, small_expenses, anomaly_report, overview_report, expense_search


@dataclass(slots=True)
class ReportRequest:
    report: str
    period: Period
    comparison: Period | None = None
    filters: ReportFilters | None = None
    top_n: int = 10
    small_threshold: Decimal | None = None
    raw_text: str = ""


class ReportService:
    """Central report layer shared by Telegram and future interfaces.

    Natural language is interpreted into a structured ReportRequest. Financial
    totals are always calculated from SQLite/report code, never by language-model
    arithmetic.
    """

    def __init__(
        self,
        db: FinanceDB,
        *,
        ai_api_key: str = "",
        ai_model: str = "",
        ai_base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.db = db
        self.ai = AIIntentInterpreter(ai_api_key, ai_model, ai_base_url)

    def parse(self, text: str, today: date) -> ReportRequest:
        raw = text.strip()
        n = normalize_text(raw)
        period = parse_period(raw, today)
        if self._implies_all_history(n) and not self._has_explicit_period(n):
            status = self.db.status()
            start = status.oldest_date or date(today.year, 1, 1)
            period = Period(start, today, "todo o histórico local")
        comparison = None

        relative_compare = self._relative_comparison(n, today)
        two_years = parse_two_years(raw)
        if relative_compare:
            comparison, period = relative_compare
        elif two_years and any(x in n for x in ("compar", " versus ", " vs ", " x ", " com ")):
            period, comparison = two_years[1], two_years[0]
        else:
            month_compare = self._month_comparison(raw, today)
            if month_compare:
                comparison, period = month_compare
            elif any(x in n for x in ("compar", "periodo anterior", "mes anterior", "mês anterior", "em relacao ao mes passado")):
                comparison = previous_equivalent(period)

        top_n = 10
        m_top = re.search(r"\b(?:top|maiores?|cinco|dez)\s*(\d+)?", n)
        if m_top:
            if m_top.group(1):
                top_n = max(1, min(50, int(m_top.group(1))))
            elif "cinco" in m_top.group(0):
                top_n = 5
            elif "dez" in m_top.group(0):
                top_n = 10
        else:
            m_num = re.search(r"\b(?:os|as|me mostre|mostre)\s+(\d{1,2})\s+(?:maiores|categorias|fornecedores)", n)
            if m_num:
                top_n = max(1, min(50, int(m_num.group(1))))

        filters = self._extract_filters(raw, period)
        small_threshold = self._extract_small_threshold(raw)

        # Specific/high-signal intents first.
        if "dre" in n or "demonstracao do resultado" in n:
            report = "dre"
        elif any(x in n for x in ("fora do padrao", "acima da media", "anomalia", "anomalias", "dispararam", "disparou", "aumentaram muito", "aumentou muito")):
            report = "anomalies"
        elif any(x in n for x in ("pequenas despesas", "despesas abaixo", "gastos abaixo", "abaixo de r$", "abaixo de ")):
            report = "small_expenses"
        elif any(x in n for x in ("quanto custa um dia", "quanto custa uma hora", "opex", "custo por dia", "custo por hora", "manter a empresa aberta por dia", "gastando por dia", "gasto por dia")):
            report = "opex"
        elif any(x in n for x in ("recorrente", "recorrentes", "assinatura", "assinaturas")):
            report = "recurring"
        elif any(x in n for x in ("pro labore", "pro-labore", "retirada", "socio", "socios", "distribuicao de lucro")):
            report = "partners"
        elif any(x in n for x in ("administracao", "administrativo", "administrativa", "custo administrativo")):
            report = "administrative"
        elif any(x in n for x in ("operacao", "operacional", "servicos de campo")):
            report = "operational"
        elif any(x in n for x in ("fixas x variaveis", "fixas e variaveis", "despesas fixas", "despesas variaveis", "percentual das minhas despesas e variavel")):
            report = "fixed_variable"
        elif "evolucao" in n or "evoluiu" in n or "evoluiram" in n:
            report = "monthly_evolution"
        elif comparison and ("categoria" in n or filters.category):
            report = "category_variation"
        elif any(x in n for x in ("cada r$ 100", "cada 100", "para onde", "distribuidas minhas despesas", "distribuicao das despesas")):
            report = "category_distribution"
        elif any(x in n for x in ("maiores fornecedores", "ranking de fornecedores", "quem recebeu mais", "fornecedores representam 80", "quanto paguei para cada fornecedor")):
            report = "supplier_ranking"
        elif self._looks_like_supplier_history(n, filters):
            report = "supplier_history"
        elif any(x in n for x in ("ranking de categorias", "categorias que mais", "categoria mais", "maiores categorias", "despesas por categoria")):
            report = "category_ranking"
        elif comparison and "compar" in n:
            report = "category_variation"
        elif any(x in n for x in ("resumo financeiro", "analise minhas despesas", "analise minhas financas", "por que estou gastando mais")):
            report = "overview"
        elif filters.category and any(x in n for x in ("quanto gastei", "quanto gasto", "gasto com", "gastei com")):
            report = "category_ranking"
            top_n = 50
        elif filters.supplier and any(x in n for x in ("quanto gastei", "quanto paguei", "historico", "gasto com")):
            report = "supplier_history"
        elif filters.text_query and any(x in n for x in ("quanto gastei", "quanto gasto", "quanto paguei", "gasto com", "gastei com", "custo anual", "custo mensal")):
            report = "expense_search"
        else:
            report = "overview"

        return ReportRequest(
            report=report,
            period=period,
            comparison=comparison,
            filters=filters,
            top_n=top_n,
            small_threshold=small_threshold,
            raw_text=raw,
        )


    @staticmethod
    def _has_explicit_period(n: str) -> bool:
        if re.search(r"\b20\d{2}\b", n):
            return True
        if re.search(r"\b(?:\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b", n):
            return True
        markers = (
            "hoje", "ontem", "esta semana", "semana passada",
            "este mes", "mes passado", "ultimos 30 dias",
            "ultimos 3 meses", "ultimos 6 meses", "ultimos 12 meses",
            "este ano", "ano passado",
        )
        if any(marker in n for marker in markers):
            return True
        return any(re.search(rf"\b{re.escape(normalize_text(name))}\b", n) for name in MONTHS)

    @staticmethod
    def _implies_all_history(n: str) -> bool:
        """Detect questions whose natural meaning is accumulated history.

        This changes only the SQLite query window; it never triggers an automatic
        historical API scan. Missing local coverage remains visible through the
        report coverage warning and can be filled with /sincronizar.
        """
        markers = (
            "quanto ja paguei",
            "quanto ja gastei",
            "historico do fornecedor",
            "historico da fornecedora",
            "historico de fornecedor",
            "historico completo",
            "desde o inicio",
            "desde o comeco",
            "desde sempre",
            "todo o historico",
        )
        return any(marker in n for marker in markers)

    @staticmethod
    def _relative_comparison(n: str, today: date) -> tuple[Period, Period] | None:
        comparing = any(x in n for x in ("compar", " em relacao ", " versus ", " vs ", " x ", " com "))
        if not comparing:
            return None
        if ("este ano" in n or "ano atual" in n) and "ano passado" in n:
            current = Period(date(today.year, 1, 1), date(today.year, 12, 31), "este ano")
            previous = Period(date(today.year - 1, 1, 1), date(today.year - 1, 12, 31), "ano passado")
            return previous, current
        if ("este mes" in n or "mes atual" in n) and ("mes passado" in n or "mes anterior" in n):
            current = Period(date(today.year, today.month, 1), month_end(today.year, today.month), "este mês")
            prev_end = current.start - timedelta(days=1)
            previous = Period(date(prev_end.year, prev_end.month, 1), prev_end, "mês passado")
            return previous, current
        return None

    @staticmethod
    def _month_comparison(raw: str, today: date) -> tuple[Period, Period] | None:
        n = normalize_text(raw)
        found: list[tuple[int, int, str]] = []
        for name, month in MONTHS.items():
            for m in re.finditer(rf"\b{re.escape(normalize_text(name))}\b", n):
                found.append((m.start(), month, name))
        found.sort(key=lambda x: x[0])
        # A month range such as "janeiro até setembro" is one period, not a
        # comparison. Only explicit comparison language splits the months.
        if len(found) < 2 or not any(x in f" {n} " for x in (" para ", " vs ", " versus ", " x ", " compar")):
            return None
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", n)]
        year = years[-1] if years else today.year
        first, second = found[0], found[-1]
        first_year = year
        second_year = year
        if first[1] > second[1] and not years:
            first_year = year - 1
        a = Period(date(first_year, first[1], 1), month_end(first_year, first[1]), f"{first[2]}/{first_year}")
        b = Period(date(second_year, second[1], 1), month_end(second_year, second[1]), f"{second[2]}/{second_year}")
        return a, b

    def _extract_filters(self, raw: str, period: Period) -> ReportFilters:
        n = normalize_text(raw)
        category = None
        # Longest matching category name wins and automatically includes descendants.
        matches = []
        for row in self.db.list_categories(active_only=True):
            desc = str(row["description"])
            nd = normalize_text(desc)
            if len(nd) >= 3 and re.search(rf"\b{re.escape(nd)}\b", n):
                matches.append(desc)
        if matches:
            category = max(matches, key=len)
        if category is None:
            # Singular/plural and small name variations, e.g. "combustível" -> "Combustíveis".
            subject_match = re.search(
                r"(?:gastei|gasto|gastos|gastamos|despesa|despesas|custo|custos)\s+(?:com|de|em)\s+(.+?)(?:\s+(?:este|esta|nos|no|na|em|desde|ultimos|últimos|de|do|da)\b|$)",
                n,
            )
            if subject_match:
                candidate = subject_match.group(1).strip()
                resolved_category = self.db.resolve_category(candidate)
                if resolved_category:
                    category = str(resolved_category["description"])

        account_id = None
        for acc in self.db.list_accounts(enabled_only=False):
            nd = normalize_text(acc.description)
            if nd and re.search(rf"\b{re.escape(nd)}\b", n):
                account_id = acc.account_id
                break

        tax_match = re.search(r"\b(?:\d[.\-/ ]*){11,14}\b", raw)
        supplier_tax_id = re.sub(r"\D", "", tax_match.group(0)) if tax_match else None

        supplier = None
        candidates = []
        patterns = [
            r"(?:paguei|gastei|gastamos)\s+(?:para|com)\s+(.+?)(?:\s+(?:em|este|esta|nos|no|na|desde|ultimos|últimos|de)\b|$)",
            r"fornecedor\s+(.+?)(?:\s+(?:em|este|esta|nos|no|na|desde|ultimos|últimos|de)\b|$)",
            r"historico\s+(?:do|da|de)\s+(.+?)(?:\s+(?:em|este|esta|nos|no|na|desde)\b|$)",
        ]
        raw_norm = normalize_text(raw)
        for pat in patterns:
            m = re.search(pat, raw_norm)
            if m:
                candidates.append(m.group(1).strip())
        for candidate in candidates:
            if category and normalize_text(candidate) in normalize_text(category):
                continue
            resolved = self.db.resolve_supplier(candidate, period.start, period.end)
            if resolved:
                supplier = resolved
                break

        min_amount = None
        max_amount = None
        value_re = r"(?:r\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
        m = re.search(rf"(?:abaixo de|ate|até|menor que)\s*{value_re}", raw_norm)
        if m:
            max_amount = self._decimal(m.group(1))
        m = re.search(rf"(?:acima de|maior que|a partir de)\s*{value_re}", raw_norm)
        if m:
            min_amount = self._decimal(m.group(1))

        text_query = None
        if not category and not supplier:
            # Preserve arbitrary subjects such as "OpenAI" even when they only
            # occur in the historic/description field. SQL still performs all
            # financial calculations.
            for candidate in candidates:
                candidate = candidate.strip(" .?!,;:")
                if len(candidate) >= 2:
                    text_query = candidate
                    break

        return ReportFilters(
            category=category,
            supplier=supplier,
            supplier_tax_id=supplier_tax_id,
            account_id=account_id,
            text_query=text_query,
            min_amount=min_amount,
            max_amount=max_amount,
        )

    @staticmethod
    def _decimal(raw: str) -> Decimal:
        text = raw.replace(".", "").replace(",", ".") if "," in raw else raw
        return Decimal(text)

    def _extract_small_threshold(self, raw: str) -> Decimal | None:
        n = normalize_text(raw)
        m = re.search(r"(?:abaixo de|ate|até|menor que)\s*(?:r\s*)?(\d+(?:[.,]\d{1,2})?)", n)
        return self._decimal(m.group(1)) if m else None

    @staticmethod
    def _looks_like_supplier_history(n: str, filters: ReportFilters) -> bool:
        if filters.supplier_tax_id:
            return True
        if not filters.supplier:
            return False
        return any(x in n for x in ("historico", "quanto paguei", "quanto ja paguei", "quanto gastei", "desde"))

    def run(self, req: ReportRequest) -> ReportResult:
        f = req.filters
        if req.report == "category_distribution":
            return category_report.distribution(self.db, req.period, top_n=req.top_n, filters=f)
        if req.report == "category_ranking":
            return category_report.ranking(self.db, req.period, top_n=req.top_n, filters=f)
        if req.report == "category_variation":
            previous = req.comparison or previous_equivalent(req.period)
            return category_report.compare(self.db, req.period, previous, filters=f)
        if req.report == "supplier_ranking":
            return supplier_report.ranking(self.db, req.period, top_n=req.top_n, filters=f)
        if req.report == "supplier_history":
            supplier = (f.supplier if f else None) or ""
            if not supplier and f and f.supplier_tax_id:
                matches = self.db.movements(start=req.period.start, end=req.period.end, direction="D", supplier_tax_id=f.supplier_tax_id, limit=1)
                supplier = matches[0].supplier_name if matches else f.supplier_tax_id
            if not supplier:
                raise ValueError("Não consegui identificar o fornecedor. Ex.: 'Quanto paguei para OpenAI em 2026?'")
            return supplier_report.history(self.db, req.period, supplier, filters=f)
        if req.report == "expense_search":
            return expense_search.run(self.db, req.period, filters=f)
        if req.report == "recurring":
            return recurring_report.run(self.db, req.period, filters=f)
        if req.report == "fixed_variable":
            return fixed_variable_report.run(self.db, req.period, filters=f)
        if req.report == "monthly_evolution":
            return monthly_evolution.run(self.db, req.period, filters=f)
        if req.report == "partners":
            return partner_report.run(self.db, req.period, filters=f)
        if req.report == "administrative":
            return administrative_expenses.run(self.db, req.period, filters=f)
        if req.report == "operational":
            return operational_expenses.run(self.db, req.period, filters=f)
        if req.report == "dre":
            return dre_report.run(self.db, req.period, filters=f)
        if req.report == "opex":
            return opex_report.run(self.db, req.period, filters=f)
        if req.report == "small_expenses":
            return small_expenses.run(self.db, req.period, threshold=req.small_threshold, filters=f)
        if req.report == "anomalies":
            return anomaly_report.run(self.db, req.period, filters=f)
        return overview_report.run(self.db, req.period, filters=f)

    def answer(self, text: str, today: date) -> str:
        interpreted = text
        if self.ai.enabled:
            # The interpreter intentionally receives no transaction values or
            # business master-data. It only normalizes the user's own wording.
            canonical = self.ai.rewrite(text, today)
            if canonical:
                interpreted = canonical
        req = self.parse(interpreted, today)
        req.raw_text = text
        result = self.run(req)
        self._attach_comparison(result, req)
        return self.format(result)

    def _attach_comparison(self, result: ReportResult, req: ReportRequest) -> None:
        if req.comparison is None or result.report_type == "category_variation":
            return
        previous_req = ReportRequest(
            report=req.report,
            period=req.comparison,
            comparison=None,
            filters=req.filters,
            top_n=req.top_n,
            small_threshold=req.small_threshold,
            raw_text=req.raw_text,
        )
        previous = self.run(previous_req)
        # Preserve coverage warnings from both periods without duplicates.
        result.warnings = list(dict.fromkeys([*result.warnings, *previous.warnings]))

        if result.report_type == "dre":
            rows = []
            labels = [
                ("gross_revenue", "Receita Bruta"),
                ("deductions", "Deduções / Impostos"),
                ("net_revenue", "Receita Líquida"),
                ("direct_costs", "Custos diretos"),
                ("gross_profit", "Lucro Bruto"),
                ("operating_expenses", "Despesas Operacionais"),
                ("operating_result", "Resultado Operacional"),
                ("net_result", "Resultado / Lucro Líquido"),
            ]
            for key, label in labels:
                cur = Decimal(result.data.get(key, 0))
                prev = Decimal(previous.data.get(key, 0))
                diff = cur - prev
                variation = (diff / prev * Decimal("100")) if prev else (Decimal("100") if cur else Decimal("0"))
                rows.append({"label": label, "current": cur, "previous": prev, "difference": diff, "difference_pct": variation})
            result.data["_dre_comparison"] = {"period": req.comparison, "rows": rows}
            return

        metric_by_type = {
            "category_distribution": ("total", "Despesas"),
            "category_ranking": ("total", "Despesas"),
            "supplier_ranking": ("total", "Despesas"),
            "supplier_history": ("total", "Total pago"),
            "expense_search": ("total", "Total gasto"),
            "recurring": ("monthly_estimate", "Custo mensal estimado"),
            "fixed_variable": ("total", "Despesas classificadas"),
            "monthly_evolution": ("total", "Despesas"),
            "partners": ("total", "Pró-labore / sócios"),
            "administrative": ("total", "Despesas administrativas"),
            "operational": ("total", "Despesas operacionais"),
            "opex": ("total", "OPEX"),
            "small_expenses": ("total", "Pequenas despesas"),
            "overview": ("expenses", "Despesas"),
        }
        spec = metric_by_type.get(result.report_type)
        if not spec:
            return
        key, label = spec
        cur = Decimal(result.data.get(key, 0))
        prev = Decimal(previous.data.get(key, 0))
        diff = cur - prev
        variation = (diff / prev * Decimal("100")) if prev else (Decimal("100") if cur else Decimal("0"))
        result.data["_comparison"] = {
            "label": label,
            "period": req.comparison,
            "current": cur,
            "previous": prev,
            "difference": diff,
            "difference_pct": variation,
        }

    def format(self, result: ReportResult) -> str:
        d = result.data
        lines = [f"📊 {result.title.upper()}", f"📅 {format_period(result.period.start, result.period.end)}", ""]
        rt = result.report_type

        if rt in {"category_distribution", "category_ranking"}:
            lines.append(f"Total de despesas: {brl(d['total'])}")
            lines.append("")
            for i, row in enumerate(d["rows"], 1):
                lines.append(f"{i}. {row['category']}")
                lines.append(f"   {brl(row['total'])} — {pct(row['percent'])} — {row['count']} lançamento(s)")
                if rt == "category_distribution":
                    lines.append(f"   A cada R$ 100 gastos: {brl(row['per_100'])}")
        elif rt == "supplier_ranking":
            lines.append(f"Total de despesas: {brl(d['total'])}")
            for i, row in enumerate(d["rows"], 1):
                lines.extend([f"{i}. {row['supplier']}", f"   {brl(row['total'])} — {pct(row['percent'])} — {row['count']} pagamento(s)", f"   Ticket médio: {brl(row['ticket_avg'])}"])
            lines.append("")
            lines.append(f"5 maiores: {pct(d['top5_percent'])} das despesas")
            lines.append(f"Fornecedores até atingir 80%: {d['pareto_80_count']}")
        elif rt == "supplier_history":
            lines += [
                f"Fornecedor: {d['supplier']}",
                f"Total pago: {brl(d['total'])}",
                f"Pagamentos: {d['count']}",
                f"Ticket médio: {brl(d['ticket_avg'])}",
                f"Média mensal (meses com movimento): {brl(d['monthly_avg'])}",
                f"Participação nas despesas: {pct(d['expense_share'])}",
                f"Primeiro pagamento: {d['first'].strftime('%d/%m/%Y') if d['first'] else '—'}",
                f"Último pagamento: {d['last'].strftime('%d/%m/%Y') if d['last'] else '—'}",
                "",
                "Principais categorias:",
            ]
            for name, value in d["categories"][:8]:
                lines.append(f"• {name}: {brl(value)}")
            if d["yearly"]:
                lines.append("")
                lines.append("Por ano:")
                for year, value in d["yearly"]:
                    lines.append(f"• {year}: {brl(value)}")
        elif rt == "expense_search":
            lines += [
                f"Filtro: {d['subject']}",
                f"Total gasto: {brl(d['total'])}",
                f"Lançamentos: {d['count']}",
                f"Ticket médio: {brl(d['ticket_avg'])}",
                f"Média mensal: {brl(d['monthly_avg'])}",
                f"% das despesas: {pct(d['expense_share'])}",
            ]
            if d["categories"]:
                lines += ["", "Categorias:"]
                for name, value in d["categories"][:8]:
                    lines.append(f"• {name}: {brl(value)}")
            if d["suppliers"]:
                lines += ["", "Fornecedores/históricos:"]
                for name, value in d["suppliers"][:8]:
                    lines.append(f"• {name}: {brl(value)}")
        elif rt == "recurring":
            lines.append(f"Custo mensal estimado: {brl(d['monthly_estimate'])}")
            lines.append("")
            if not d["rows"]:
                lines.append("Nenhuma recorrência com confiança suficiente foi detectada no período.")
            for i, row in enumerate(d["rows"][:15], 1):
                lines += [
                    f"{i}. {row['supplier']}",
                    f"   {row['frequency']} — média {brl(row['average'])}",
                    f"   Estimado/mês: {brl(row['monthly_estimate'])} | ano: {brl(row['annual_estimate'])}",
                    f"   Confiança: {row['confidence']} | último: {row['last'].strftime('%d/%m/%Y')}",
                ]
        elif rt == "fixed_variable":
            lines.append(f"Total: {brl(d['total'])}")
            for row in d["rows"]:
                lines.append(f"• {row['label']}: {brl(row['total'])} — {pct(row['percent'])} — média {brl(row['monthly_avg'])}/mês")
        elif rt == "monthly_evolution":
            lines += [f"Total: {brl(d['total'])}", f"Média mensal: {brl(d['average'])}", f"Tendência: {d['trend']}", ""]
            for row in d["rows"]:
                lines.append(f"• {row['month']}: {brl(row['total'])} ({signed_pct(row['variation_pct'])})")
            if d["max"]:
                lines.append(f"Maior mês: {d['max']['month']} — {brl(d['max']['total'])}")
            if d["min"]:
                lines.append(f"Menor mês: {d['min']['month']} — {brl(d['min']['total'])}")
        elif rt == "category_variation":
            prev = d["previous_period"]
            lines.append(f"Comparação: {format_period(prev.start, prev.end)}")
            lines.append("")
            for row in d["rows"][:15]:
                lines += [
                    f"• {row['category']}",
                    f"  Atual: {brl(row['current'])} | anterior: {brl(row['previous'])}",
                    f"  Diferença: {signed_brl(row['difference'])} ({signed_pct(row['difference_pct'])})",
                ]
        elif rt == "partners":
            lines += [f"Total: {brl(d['total'])}", f"Média mensal: {brl(d['monthly_avg'])}", f"% da receita: {pct(d['revenue_share'])}", ""]
            for person, value in d["people"]:
                lines.append(f"• {person}: {brl(value)}")
        elif rt in {"administrative", "operational"}:
            lines += [
                f"Total: {brl(d['total'])}",
                f"Média mensal: {brl(d['monthly_avg'])}",
                f"% da receita: {pct(d['revenue_share'])}",
                f"% das despesas: {pct(d['expense_share'])}",
                "",
                "Principais categorias:",
            ]
            for name, value in d["categories"][:10]:
                lines.append(f"• {name}: {brl(value)}")
        elif rt == "dre":
            lines += [
                f"Receita Bruta: {brl(d['gross_revenue'])}",
                f"(-) Deduções / Impostos: {brl(d['deductions'])}",
                f"= Receita Líquida: {brl(d['net_revenue'])}",
                f"(-) Custos diretos: {brl(d['direct_costs'])}",
                f"= Lucro Bruto: {brl(d['gross_profit'])} ({pct(d['gross_margin'])})",
                f"(-) Despesas Operacionais: {brl(d['operating_expenses'])}",
                f"= Resultado Operacional: {brl(d['operating_result'])} ({pct(d['operating_margin'])})",
                f"(+) Outras receitas: {brl(d['other_income'])}",
                f"(-) Outras despesas: {brl(d['other_expenses'])}",
                f"= Resultado / Lucro Líquido: {brl(d['net_result'])} ({pct(d['net_margin'])})",
            ]
        elif rt == "opex":
            lines += [
                f"OPEX total: {brl(d['total'])}",
                f"OPEX médio mensal: {brl(d['monthly_avg'])}",
                f"Custo por dia útil ({d['workdays']} dias): {brl(d['per_day'])}",
                f"Custo por hora ({d['hours_per_day']} h/dia): {brl(d['per_hour'])}",
                f"% da receita: {pct(d['revenue_share'])}",
                "",
                "Principais categorias:",
            ]
            for name, value in d["categories"][:10]:
                lines.append(f"• {name}: {brl(value)}")
        elif rt == "small_expenses":
            lines += [
                f"Limite: até {brl(d['threshold'])}",
                f"Lançamentos: {d['count']}",
                f"Total: {brl(d['total'])}",
                f"Ticket médio: {brl(d['ticket_avg'])}",
                f"% das despesas: {pct(d['expense_share'])}",
                "",
                "Categorias mais envolvidas:",
            ]
            for name, value in d["categories"][:8]:
                lines.append(f"• {name}: {brl(value)}")
            if d["frequent"]:
                lines.append("")
                lines.append("Mais frequentes:")
                for name, count in d["frequent"][:8]:
                    lines.append(f"• {name}: {count} lançamento(s)")
        elif rt == "anomalies":
            lines.append(f"Referência: média dos {d['baseline_months']} meses anteriores")
            lines.append(f"Critério: +{pct(d['pct_threshold'])} e pelo menos {brl(d['min_amount'])}")
            lines.append("")
            if not d["rows"]:
                lines.append("Nenhuma categoria ultrapassou os critérios configurados.")
            for row in d["rows"][:15]:
                lines += [
                    f"• {row['category']}",
                    f"  Média: {brl(row['baseline_avg'])}",
                    f"  Atual: {brl(row['current'])}",
                    f"  Variação: {signed_brl(row['difference'])} ({signed_pct(row['variation_pct'])})",
                ]
        else:  # overview
            lines += [
                f"Receitas: {brl(d['revenue'])}",
                f"Despesas: {brl(d['expenses'])}",
                f"Resultado: {brl(d['result'])}",
                f"Margem sobre entradas: {pct(d['margin'])}",
                f"Receita média mensal: {brl(d['monthly_revenue_avg'])}",
                f"Despesa média mensal: {brl(d['monthly_expense_avg'])}",
                f"Administrativo: {brl(d['administrative'])}",
                f"Operacional: {brl(d['operational'])}",
                f"OPEX classificado: {brl(d['opex'])}",
                f"Pequenas despesas (≤ {brl(d['small_threshold'])}): {brl(d['small_total'])} em {d['small_count']} lançamentos",
                "",
                f"Despesas no período anterior: {brl(d['previous_expenses'])}",
                f"Variação das despesas: {signed_brl(d['expense_difference'])} ({signed_pct(d['expense_change_pct'])})",
                "",
                "Maiores categorias:",
            ]
            for name, value in d["top_categories"]:
                lines.append(f"• {name}: {brl(value)}")
            lines.append("")
            lines.append("Maiores fornecedores:")
            for name, value in d["top_suppliers"]:
                lines.append(f"• {name}: {brl(value)}")
            if d.get("top_increases"):
                lines.append("")
                lines.append("Categorias que mais aumentaram vs. período anterior:")
                for name, current, previous, diff, variation in d["top_increases"]:
                    lines.append(f"• {name}: {signed_brl(diff)} ({signed_pct(variation)})")

        dre_comparison = d.get("_dre_comparison")
        if dre_comparison:
            lines += ["", "📈 COMPARAÇÃO DRE", f"Referência: {format_period(dre_comparison['period'].start, dre_comparison['period'].end)}"]
            for row in dre_comparison["rows"]:
                lines += [
                    f"• {row['label']}",
                    f"  Atual: {brl(row['current'])} | anterior: {brl(row['previous'])}",
                    f"  Diferença: {signed_brl(row['difference'])} ({signed_pct(row['difference_pct'])})",
                ]
        comparison = d.get("_comparison")
        if comparison:
            lines += [
                "",
                "📈 COMPARAÇÃO",
                f"Referência: {format_period(comparison['period'].start, comparison['period'].end)}",
                f"{comparison['label']} — atual: {brl(comparison['current'])}",
                f"Anterior: {brl(comparison['previous'])}",
                f"Diferença: {signed_brl(comparison['difference'])} ({signed_pct(comparison['difference_pct'])})",
            ]

        if result.warnings:
            lines.append("")
            lines.extend(result.warnings)
        return "\n".join(lines)
