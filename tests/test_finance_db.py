import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_db import FinanceDB


class FinanceDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = FinanceDB(Path(self.tmp.name) / "financeiro.db")
        self.db.upsert_account_catalog([
            {"account_id": "1", "description": "Bling Conta"},
            {"account_id": "2", "description": "Infinity Bank"},
        ])
        self.db.reconcile_auto_accounts(["1", "2"])
        self.db.upsert_categories([
            {"category_id": "10", "description": "Software", "category_type": 2, "active": True},
            {"category_id": "11", "description": "Combustíveis", "category_type": 2, "active": True},
        ])
        self.db.auto_classify_unmapped()
        self.db.replace_period(
            date(2026, 9, 1),
            date(2026, 9, 30),
            [
                {"movement_id": "a", "account_id": "1", "account_name": "Bling Conta", "movement_date": date(2026,9,10), "direction": "D", "amount": Decimal("100.00"), "category_id": "10", "category_name": "Software", "supplier_name": "OpenAI", "supplier_tax_id": "", "description": "assinatura", "affects_balance": True, "status": "R"},
                {"movement_id": "b", "account_id": "2", "account_name": "Infinity Bank", "movement_date": date(2026,9,12), "direction": "D", "amount": Decimal("200.00"), "category_id": "11", "category_name": "Combustíveis", "supplier_name": "Posto X", "supplier_tax_id": "", "description": "gasolina", "affects_balance": True, "status": "R"},
                {"movement_id": "c", "account_id": "1", "account_name": "Bling Conta", "movement_date": date(2026,9,15), "direction": "C", "amount": Decimal("1000.00"), "category_id": None, "category_name": "Vendas", "supplier_name": "Cliente", "supplier_tax_id": "", "description": "recebimento", "affects_balance": True, "status": "R"},
            ],
            discover_since=date(2026, 9, 1),
            sync_type="test",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_category_descendants_and_filters(self):
        rows = self.db.movements(start=date(2026,9,1), end=date(2026,9,30), direction="D", category_query="Software")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].supplier_name, "OpenAI")

    def test_balance_calibration_avoids_full_history(self):
        self.db.calibrate_balances({"Bling Conta": Decimal("900.00"), "Infinity Bank": Decimal("300.00")}, date(2026,9,30))
        balances = self.db.balances_as_of(date(2026,9,30))
        values = {b.account.description: b.balance for b in balances}
        self.assertEqual(values["Bling Conta"], Decimal("900.00"))
        self.assertEqual(values["Infinity Bank"], Decimal("300.00"))


    def test_category_name_is_linked_to_catalog_when_id_is_missing(self):
        self.db.replace_period(
            date(2026, 10, 1),
            date(2026, 10, 1),
            [{
                "movement_id": "name-only",
                "account_id": "1",
                "account_name": "Bling Conta",
                "movement_date": date(2026, 10, 1),
                "direction": "D",
                "amount": Decimal("55.00"),
                "category_id": None,
                "category_name": "Software",
                "supplier_name": "Fornecedor",
                "supplier_tax_id": "",
                "description": "teste",
                "affects_balance": True,
                "status": "R",
            }],
            sync_type="test",
        )
        rows = self.db.movements(start=date(2026,10,1), end=date(2026,10,1), category_query="Software")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category_id, "10")

    def test_auto_classification_is_editable(self):
        row = next(r for r in self.db.classification_rows() if r["description"] == "Software")
        self.assertEqual(row["managerial_group"], "administrative")
        self.db.set_category_classification("Software", managerial_group="operational", is_opex=True)
        row2 = next(r for r in self.db.classification_rows() if r["description"] == "Software")
        self.assertEqual(row2["managerial_group"], "operational")
        self.assertEqual(row2["is_opex"], 1)


if __name__ == "__main__":
    unittest.main()
