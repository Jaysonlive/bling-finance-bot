import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from bling import BlingClient
from finance_db import FinanceDB


class EntryPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.client = BlingClient(
            "client",
            "secret",
            root / "tokens.json",
            cash_db_file=root / "financeiro.db",
        )
        self.calls = []

        async def fake_request(method, path, *, params=None, json_body=None):
            self.calls.append((method, path, params, json_body))
            return {"data": {"id": 987654321}}

        self.client._request = fake_request

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def test_create_payable_includes_essential_fields(self):
        created = await self.client.create_payable(
            contact_id="123",
            amount=Decimal("1500.90"),
            due_date=date(2026, 10, 10),
            competence=date(2026, 9, 24),
            category_id="456",
            history="NF fornecedor",
            financial_account_id="789",
            emission_date=date(2026, 9, 24),
        )
        self.assertEqual(created, "987654321")
        method, path, _, body = self.calls[-1]
        self.assertEqual((method, path), ("POST", "/contas/pagar"))
        self.assertEqual(body["contato"]["id"], 123)
        self.assertEqual(body["categoria"]["id"], 456)
        self.assertEqual(body["competencia"], "2026-09-24")
        self.assertEqual(body["vencimento"], "2026-10-10")
        self.assertEqual(body["portador"]["id"], 789)
        self.assertEqual(body["ocorrencia"], {"tipo": 1})

    async def test_create_receivable_includes_client_category_and_competence(self):
        await self.client.create_receivable(
            contact_id="321",
            amount=Decimal("800.00"),
            due_date=date(2026, 10, 5),
            competence=date(2026, 9, 24),
            category_id="654",
            history="Mensalidade",
            financial_account_id=None,
            emission_date=date(2026, 9, 24),
        )
        _, path, _, body = self.calls[-1]
        self.assertEqual(path, "/contas/receber")
        self.assertEqual(body["contato"]["id"], 321)
        self.assertEqual(body["categoria"]["id"], 654)
        self.assertEqual(body["competencia"], "2026-09-24")
        self.assertNotIn("portador", body)

    async def test_create_cash_entry_has_account_contact_category_and_direction(self):
        async def fake_resync(start, end):
            self.assertEqual(start, date(2026, 9, 24))
            self.assertEqual(end, date(2026, 9, 24))
            return 1

        self.client.resync_cash_period = fake_resync
        await self.client.create_cash_entry(
            contact_id="111",
            amount=Decimal("49.90"),
            movement_date=date(2026, 9, 24),
            competence=date(2026, 9, 24),
            category_id="222",
            financial_account_id="333",
            direction="D",
            history="Pagamento à vista",
        )
        _, path, _, body = self.calls[-1]
        self.assertEqual(path, "/caixas")
        self.assertEqual(body["debCred"], "D")
        self.assertEqual(body["idContaContabil"], 333)
        self.assertNotIn("contaFinanceira", body)
        self.assertEqual(body["contato"]["id"], 111)
        self.assertEqual(body["categoria"]["id"], 222)
        self.assertEqual(body["competencia"], "2026-09-24")


class CategorySearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = FinanceDB(Path(self.tmp.name) / "financeiro.db")
        self.db.upsert_categories(
            [
                {"category_id": "1", "description": "Software", "category_type": 1, "active": True},
                {"category_id": "2", "description": "Venda de serviços", "category_type": 2, "active": True},
                {"category_id": "3", "description": "Outros", "category_type": 3, "active": True},
            ]
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_expense_picker_does_not_offer_revenue_only_category(self):
        rows = self.db.search_categories("venda", category_type=1)
        self.assertFalse(any(r["category_id"] == "2" for r in rows))

    def test_revenue_picker_does_not_offer_expense_only_category(self):
        rows = self.db.search_categories("software", category_type=2)
        self.assertFalse(any(r["category_id"] == "1" for r in rows))

    def test_both_type_is_eligible(self):
        rows = self.db.search_categories("outros", category_type=1)
        self.assertEqual(rows[0]["category_id"], "3")


if __name__ == "__main__":
    unittest.main()
