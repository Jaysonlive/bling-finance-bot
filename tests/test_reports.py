import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from finance_db import FinanceDB
from services import ReportService


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db = FinanceDB(Path(self.tmp.name) / "financeiro.db")
        self.db = db
        db.upsert_account_catalog([{"account_id":"1","description":"Caixa"}])
        db.reconcile_auto_accounts(["1"])
        db.upsert_categories([
            {"category_id":"1","description":"Software","category_type":2,"active":True},
            {"category_id":"2","description":"Combustíveis","category_type":2,"active":True},
            {"category_id":"3","description":"Vendas de serviços","category_type":1,"active":True},
        ])
        db.auto_classify_unmapped()
        rows = []
        mid = 0
        for month in range(1, 10):
            mid += 1
            rows.append({"movement_id":f"r{mid}","account_id":"1","account_name":"Caixa","movement_date":date(2026,month,5),"direction":"C","amount":Decimal("1000"),"category_id":"3","category_name":"Vendas de serviços","supplier_name":"Cliente","supplier_tax_id":"","description":"receita","affects_balance":True,"status":"R"})
            mid += 1
            rows.append({"movement_id":f"d{mid}","account_id":"1","account_name":"Caixa","movement_date":date(2026,month,10),"direction":"D","amount":Decimal(str(100+month)),"category_id":"1","category_name":"Software","supplier_name":"OpenAI","supplier_tax_id":"","description":"assinatura OpenAI","affects_balance":True,"status":"R"})
            mid += 1
            rows.append({"movement_id":f"f{mid}","account_id":"1","account_name":"Caixa","movement_date":date(2026,month,15),"direction":"D","amount":Decimal(str(200+month*10)),"category_id":"2","category_name":"Combustíveis","supplier_name":"Posto X","supplier_tax_id":"","description":"gasolina","affects_balance":True,"status":"R"})
        db.replace_period(date(2026,1,1), date(2026,9,30), rows, discover_since=date(2026,1,1), sync_type="test")
        self.service = ReportService(db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_natural_language_category(self):
        text = self.service.answer("Quanto gastei com software em 2026?", date(2026,9,22))
        self.assertIn("SOFTWARE", text.upper())
        self.assertIn("R$", text)

    def test_supplier_ranking(self):
        text = self.service.answer("Me mostre os 5 maiores fornecedores de 2026", date(2026,9,22))
        self.assertIn("OPENAI", text.upper())
        self.assertIn("POSTO X", text.upper())

    def test_dre(self):
        text = self.service.answer("Me mostre a DRE de janeiro até setembro de 2026", date(2026,9,22))
        self.assertIn("RECEITA BRUTA", text.upper())
        self.assertIn("LUCRO", text.upper())

    def test_recurring_detection(self):
        text = self.service.answer("Quais são minhas despesas recorrentes em 2026?", date(2026,9,22))
        self.assertIn("OPENAI", text.upper())

    def test_free_text_expense_search(self):
        text = self.service.answer("Quanto gastei com OpenAI nos últimos 12 meses?", date(2026,9,22))
        self.assertIn("OPENAI", text.upper())
        self.assertIn("TOTAL", text.upper())

    def test_relative_period_comparison(self):
        req = self.service.parse("Compare meu custo operacional deste ano com o ano passado", date(2026,9,22))
        self.assertEqual(req.period.start, date(2026,1,1))
        self.assertIsNotNone(req.comparison)
        self.assertEqual(req.comparison.start, date(2025,1,1))
        self.assertEqual(req.report, "operational")

    def test_dre_comparison(self):
        text = self.service.answer("Compare a DRE de 2025 com 2026", date(2026,9,22))
        self.assertIn("COMPARAÇÃO DRE", text.upper())
        self.assertIn("RECEITA BRUTA", text.upper())

    def test_month_range_is_not_misread_as_comparison(self):
        req = self.service.parse("Me mostre a DRE de janeiro até setembro de 2026", date(2026,9,22))
        self.assertEqual(req.period.start, date(2026,1,1))
        self.assertEqual(req.period.end, date(2026,9,30))
        self.assertIsNone(req.comparison)

    def test_accumulated_supplier_question_uses_local_history(self):
        req = self.service.parse("Quanto já paguei para OpenAI?", date(2026,9,22))
        self.assertEqual(req.period.start, date(2026,1,5))
        self.assertEqual(req.period.end, date(2026,9,22))
        self.assertEqual(req.report, "supplier_history")
        self.assertEqual(req.filters.supplier, "OpenAI")

    def test_accumulated_supplier_with_explicit_year_keeps_year(self):
        req = self.service.parse("Quanto já paguei para OpenAI em 2026?", date(2026,9,22))
        self.assertEqual(req.period.start, date(2026,1,1))
        self.assertEqual(req.period.end, date(2026,12,31))
        self.assertEqual(req.report, "supplier_history")

    def test_small_expense_threshold_with_brl_prefix(self):
        req = self.service.parse("Quanto gastei em despesas abaixo de R$ 1000 este ano?", date(2026,9,22))
        self.assertEqual(req.report, "small_expenses")
        self.assertEqual(req.small_threshold, Decimal("1000"))

    def test_since_year_runs_until_today(self):
        req = self.service.parse("Quanto gastamos com OpenAI desde 2024?", date(2026,9,22))
        self.assertEqual(req.period.start, date(2024,1,1))
        self.assertEqual(req.period.end, date(2026,9,22))

    def test_category_comparison_keeps_category_filter(self):
        req = self.service.parse("Compare os gastos com combustíveis de 2025 e 2026", date(2026,9,22))
        self.assertEqual(req.report, "category_variation")
        self.assertEqual(req.filters.category, "Combustíveis")
        self.assertEqual(req.period.start, date(2026,1,1))
        self.assertEqual(req.comparison.start, date(2025,1,1))


if __name__ == "__main__":
    unittest.main()
