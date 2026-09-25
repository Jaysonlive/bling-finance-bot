import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from bling import BlingClient


class FinancialCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.client = BlingClient(
            "client",
            "secret",
            root / "tokens.json",
            cash_db_file=root / "financeiro.db",
        )

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_receivable_filter_is_sent_to_bling(self):
        calls = []

        async def fake_paginate(path, params):
            calls.append((path, list(params)))
            return []

        self.client._paginate = fake_paginate
        await self.client.list_open_receivables(
            date(2026, 9, 1), date(2026, 9, 30), contact_id="12345"
        )
        self.assertEqual(calls[0][0], "/contas/receber")
        self.assertIn(("idContato", 12345), calls[0][1])
        self.assertIn(("tipoFiltroData", "V"), calls[0][1])

    async def test_payable_filter_is_sent_for_open_and_partial(self):
        calls = []

        async def fake_paginate(path, params):
            calls.append((path, list(params)))
            return []

        self.client._paginate = fake_paginate
        await self.client.list_open_payables(
            date(2026, 9, 1), date(2026, 9, 30), contact_id=987
        )
        self.assertEqual(len(calls), 2)
        for path, params in calls:
            self.assertEqual(path, "/contas/pagar")
            self.assertIn(("idContato", 987), params)
        statuses = {dict(params)["situacao"] for _, params in calls}
        self.assertEqual(statuses, {1, 3})

    async def test_card_uses_remaining_balance_for_partial_title(self):
        async def fake_payables(start, end, *, contact_id=None):
            self.assertEqual(contact_id, "777")
            return [
                {
                    "id": 1,
                    "situacao": 1,
                    "vencimento": "2026-09-10",
                    "valor": 1000.0,
                },
                {
                    "id": 2,
                    "situacao": 3,
                    "vencimento": "2026-09-20",
                    "valor": 800.0,
                },
            ]

        async def fake_detail(kind, account_id):
            self.assertEqual(kind, "pagar")
            self.assertEqual(str(account_id), "2")
            return {"saldo": 250.5}

        self.client.list_open_payables = fake_payables
        self.client._get_account_detail = fake_detail

        card = await self.client.get_contact_account_card(
            "payable", "777", date(2026, 9, 1), date(2026, 9, 30)
        )
        self.assertEqual(len(card.items), 2)
        self.assertEqual(card.items[0].outstanding, Decimal("1000.0"))
        self.assertEqual(card.items[1].outstanding, Decimal("250.5"))
        self.assertTrue(card.items[1].partial)
        self.assertEqual(card.total, Decimal("1250.5"))

    async def test_card_sorts_titles_by_due_date(self):
        async def fake_receivables(start, end, *, contact_id=None):
            return [
                {"id": 2, "situacao": 1, "vencimento": "2026-10-20", "valor": 200},
                {"id": 1, "situacao": 1, "vencimento": "2026-10-05", "valor": 100},
            ]

        self.client.list_open_receivables = fake_receivables
        card = await self.client.get_contact_account_card(
            "receivable", "9", date(2026, 10, 1), date(2026, 10, 31)
        )
        self.assertEqual([x.account_id for x in card.items], ["1", "2"])
        self.assertEqual(card.total, Decimal("300"))


if __name__ == "__main__":
    unittest.main()
