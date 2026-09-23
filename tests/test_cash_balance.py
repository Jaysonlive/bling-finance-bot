import unittest
from datetime import date
from decimal import Decimal

from bling import BlingClient


class FakeBlingClient(BlingClient):
    def __init__(self):
        super().__init__(
            "client",
            "secret",
            "/tmp/unused-tokens-v5.json",
            cash_history_start=date(2026, 8, 1),
        )

    async def list_financial_accounts(self):
        # Only this account exists in the current Bling account catalog.
        return {"1": "Infinity Bank"}

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
                "saldo": "S",
                "situacao": "R",
            },
            {
                "id": "credit",
                "data": "2026-09-10",
                "debcred": "C",
                "valor": 12037.33,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
                "saldo": "S",
                "situacao": "R",
            },
            {
                "id": "debit",
                "data": "2026-09-20",
                "debcred": "D",
                "valor": 11744.19,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
                "saldo": "S",
                "situacao": "R",
            },
            # Exists in reports, but must not affect the balance.
            {
                "id": "no-balance",
                "data": "2026-09-21",
                "debcred": "D",
                "valor": 999.99,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
                "saldo": "N",
                "situacao": "R",
            },
            # Excluded movement must not affect the balance either.
            {
                "id": "excluded",
                "data": "2026-09-21",
                "debcred": "C",
                "valor": 500.00,
                "contafinanceira_id": 1,
                "contafinanceira_descricao": "Infinity Bank",
                "saldo": "S",
                "situacao": "E",
            },
            # Historical account no longer present in /contas-contabeis.
            {
                "id": "old-account",
                "data": "2026-08-15",
                "debcred": "D",
                "valor": 355.55,
                "contafinanceira_id": 2,
                "contafinanceira_descricao": "Itaú Empresas",
                "saldo": "S",
                "situacao": "R",
            },
        ]
        return [row for row in rows if start <= date.fromisoformat(row["data"]) <= end]


class CashBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_catalog_and_balance_flags_are_respected(self):
        client = FakeBlingClient()
        try:
            summary = await client.get_cash_summary(date(2026, 9, 22))
            self.assertEqual(
                client.requested_ranges,
                [(date(2026, 8, 1), date(2026, 9, 22))],
            )
            self.assertEqual(len(summary.accounts), 1)
            self.assertEqual(summary.accounts[0].description, "Infinity Bank")
            self.assertEqual(summary.accounts[0].balance, Decimal("377.14"))
            self.assertEqual(summary.accounts[0].movement_count, 3)
            self.assertNotIn("Itaú Empresas", [a.description for a in summary.accounts])
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
