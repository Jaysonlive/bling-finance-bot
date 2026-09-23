from finance_db import FinanceDB
from .base import ReportFilters
from .common import Period
from .category_report import compare


def run(db: FinanceDB, current: Period, previous: Period, *, filters: ReportFilters | None = None):
    return compare(db, current, previous, filters=filters)
