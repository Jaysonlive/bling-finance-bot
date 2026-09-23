from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


MONTHS = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5, "mai": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}


def norm(value: str) -> str:
    raw = unicodedata.normalize("NFKD", value or "")
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.casefold()
    raw = re.sub(r"[^a-z0-9/\-]+", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def add_months(d: date, months: int) -> date:
    idx = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(idx, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


@dataclass(frozen=True, slots=True)
class ComparisonPeriod:
    current: Period
    previous: Period


def previous_equivalent(period: Period) -> Period:
    days = period.days
    end = period.start - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    return Period(start, end, f"período anterior ({format_period(start, end)})")


def format_period(start: date, end: date) -> str:
    if start == end:
        return start.strftime("%d/%m/%Y")
    return f"{start.strftime('%d/%m/%Y')} a {end.strftime('%d/%m/%Y')}"


def _parse_date_token(raw: str) -> date | None:
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    return None


def parse_period(text: str | None, today: date) -> Period:
    """Parse Portuguese business-period expressions deterministically."""
    raw = (text or "").strip()
    s = norm(raw)

    # Explicit date interval.
    tokens = re.findall(r"\b(?:\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b", raw)
    if len(tokens) >= 2:
        a = _parse_date_token(tokens[0])
        b = _parse_date_token(tokens[1])
        if a and b:
            if b < a:
                a, b = b, a
            return Period(a, b, format_period(a, b))

    # Specific year, e.g. 2025.
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", s)]

    # Accumulated interval such as "desde 2024" means from the beginning
    # of that year through today, rather than only the calendar year 2024.
    since_year = re.search(r"\bdesde\s+(20\d{2})\b", s)
    if since_year:
        y = int(since_year.group(1))
        start = date(y, 1, 1)
        end = today if today >= start else date(y, 12, 31)
        return Period(start, end, f"desde {y}")

    if s in {"hoje", "de hoje"} or " hoje" in f" {s}":
        return Period(today, today, "hoje")
    if "ontem" in s:
        d = today - timedelta(days=1)
        return Period(d, d, "ontem")
    if "semana passada" in s:
        current_start = today - timedelta(days=today.weekday())
        start = current_start - timedelta(days=7)
        end = start + timedelta(days=6)
        return Period(start, end, "semana passada")
    if "esta semana" in s or "semana atual" in s:
        start = today - timedelta(days=today.weekday())
        return Period(start, start + timedelta(days=6), "esta semana")
    if "mes passado" in s:
        prev = add_months(date(today.year, today.month, 1), -1)
        return Period(date(prev.year, prev.month, 1), month_end(prev.year, prev.month), "mês passado")
    if "este mes" in s or "mes atual" in s or s == "mes":
        return Period(date(today.year, today.month, 1), month_end(today.year, today.month), "este mês")
    if "ultimos 30 dias" in s or "ultimas 30 dias" in s:
        return Period(today - timedelta(days=29), today, "últimos 30 dias")

    m = re.search(r"ultim[oa]s?\s+(3|6|12)\s+mes", s)
    if m:
        n = int(m.group(1))
        start = add_months(today, -n) + timedelta(days=1)
        return Period(start, today, f"últimos {n} meses")

    if "ano passado" in s:
        y = today.year - 1
        return Period(date(y, 1, 1), date(y, 12, 31), "ano passado")
    if "este ano" in s or "ano atual" in s:
        return Period(date(today.year, 1, 1), date(today.year, 12, 31), "este ano")

    # Month range, e.g. "janeiro até setembro de 2026" or "jan a set".
    month_tokens: list[tuple[int, int, str]] = []
    for month_name, month in MONTHS.items():
        for match in re.finditer(rf"\b{re.escape(norm(month_name))}\b", s):
            month_tokens.append((match.start(), month, month_name))
    month_tokens.sort(key=lambda x: x[0])
    if len(month_tokens) >= 2 and any(word in s for word in (" ate ", " a ", " para ", " entre ")):
        first = month_tokens[0]
        last = month_tokens[-1]
        year = years[-1] if years else today.year
        start = date(year, first[1], 1)
        end = month_end(year, last[1])
        if end < start:
            # Cross-year range such as novembro a fevereiro.
            end = month_end(year + 1, last[1])
        return Period(start, end, f"{first[2]} a {last[2]}/{end.year}")

    # Month name, optionally with year.
    for month_name, month in MONTHS.items():
        if re.search(rf"\b{re.escape(norm(month_name))}\b", s):
            year = years[0] if years else today.year
            return Period(date(year, month, 1), month_end(year, month), f"{month_name}/{year}")

    if years:
        y = years[0]
        return Period(date(y, 1, 1), date(y, 12, 31), str(y))

    # Default is current month; report messages explicitly show it.
    return Period(date(today.year, today.month, 1), month_end(today.year, today.month), "este mês")



def parse_two_months(text: str, today: date) -> tuple[Period, Period] | None:
    """Parse comparisons such as "agosto para setembro" or "ago x set de 2026"."""
    s = norm(text)
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", s)]
    found: list[tuple[int, int, str]] = []
    # Avoid duplicates from aliases (e.g. "mar" inside "marco") by keeping the
    # longest token at the same start position.
    candidates: list[tuple[int, int, str, int]] = []
    for name, month in MONTHS.items():
        nn = norm(name)
        for m in re.finditer(rf"\b{re.escape(nn)}\b", s):
            candidates.append((m.start(), month, name, len(nn)))
    by_pos: dict[int, tuple[int, int, str, int]] = {}
    for item in candidates:
        if item[0] not in by_pos or item[3] > by_pos[item[0]][3]:
            by_pos[item[0]] = item
    for pos in sorted(by_pos):
        item = by_pos[pos]
        found.append((item[0], item[1], item[2]))
    if len(found) < 2:
        return None
    # Only treat it as two independent periods when comparison language is
    # present. Otherwise parse_period can treat it as a month range.
    if not any(token in f" {s} " for token in (" compar", " versus ", " vs ", " x ", " para ")):
        return None
    a, b = found[0], found[1]
    if len(years) >= 2:
        ya, yb = years[0], years[1]
    else:
        y = years[-1] if years else today.year
        ya = y
        yb = y + 1 if b[1] < a[1] else y
    pa = Period(date(ya, a[1], 1), month_end(ya, a[1]), f"{a[2]}/{ya}")
    pb = Period(date(yb, b[1], 1), month_end(yb, b[1]), f"{b[2]}/{yb}")
    return pa, pb


def parse_two_years(text: str) -> tuple[Period, Period] | None:
    years = [int(x) for x in re.findall(r"\b(20\d{2})\b", norm(text))]
    if len(years) >= 2 and years[0] != years[1]:
        a, b = years[0], years[1]
        return (
            Period(date(a, 1, 1), date(a, 12, 31), str(a)),
            Period(date(b, 1, 1), date(b, 12, 31), str(b)),
        )
    return None


def safe_pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return (numerator / denominator * Decimal("100"))


def month_keys(start: date, end: date) -> list[tuple[int, int]]:
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    out: list[tuple[int, int]] = []
    while cursor <= last:
        out.append((cursor.year, cursor.month))
        cursor = add_months(cursor, 1)
    return out
