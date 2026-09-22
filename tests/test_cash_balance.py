import unittest
from datetime import date
from decimal import Decimal

from bling import BlingClient


class FakeBlingClient(BlingClient):
    def __init__(self):
        super().__init__(
            "client",
            "secret",
            "/tmp/unused-tokens.json",
            cash_history_start=date(2026, 8, 1),
        )

    async def list_cash_entries(self, start: date, end: date):
        self.requested_ranges = getattr(self, "requested_ranges", []) + [(start, end)]
        rows = [
            {
                "id": "opening",
                "data": "2026-08-31",
                "debcred": "C",
                "valor": 84.00,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
            },
            {
                "id": "credit",
                "data": "2026-09-10",
                "debcred": "C",
                "valor": 12037.33,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
            },
            {
                "id": "debit",
                "data": "2026-09-20",
                "debcred": "D",
                "valor": 11744.19,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
            },
        ]
        return [row for row in rows if start <= date.fromisoformat(row["data"]) <= end]


class CashBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_includes_opening_history(self):
        client = FakeBlingClient()
        try:
            summary = await client.get_cash_summary(date(2026, 9, 22))
            self.assertEqual(
                client.requested_ranges,
                [
                    (date(2026, 8, 1), date(2026, 9, 22)),
                    (date(2026, 9, 1), date(2026, 9, 22)),
                ],
            )
            self.assertEqual(len(summary.accounts), 1)
            self.assertEqual(summary.accounts[0].balance, Decimal("377.14"))
        finally:
            await client.close()

    def test_long_ranges_are_split_without_gaps(self):
        ranges = list(BlingClient._split_date_range(date(2024, 1, 1), date(2026, 9, 22)))
        self.assertEqual(ranges[0][0], date(2024, 1, 1))
        self.assertEqual(ranges[-1][1], date(2026, 9, 22))
        for previous, current in zip(ranges, ranges[1:]):
            self.assertEqual(previous[1].toordinal() + 1, current[0].toordinal())
            self.assertLessEqual((previous[1] - previous[0]).days, 364)


if __name__ == "__main__":
    unittest.main()
