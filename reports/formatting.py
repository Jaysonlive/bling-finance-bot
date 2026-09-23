from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def brl(value: Decimal | int | float) -> str:
    value = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    negative = value < 0
    raw = f"{abs(value):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if negative else ''}R$ {raw}"


def pct(value: Decimal | int | float) -> str:
    value = Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{value:.1f}%".replace(".", ",")


def signed_brl(value: Decimal) -> str:
    return ("+" if value > 0 else "") + brl(value)


def signed_pct(value: Decimal) -> str:
    return ("+" if value > 0 else "") + pct(value)
