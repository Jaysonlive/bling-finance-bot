from finance_db import FinanceDB
from .base import ReportFilters
from .common import Period
from .group_expenses import run_group


def run(db: FinanceDB, period: Period, *, filters: ReportFilters | None = None):
    return run_group(db, period, managerial_group="administrative", title="Despesas administrativas", filters=filters)
