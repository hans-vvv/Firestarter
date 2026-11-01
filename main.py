from __future__ import annotations

from pathlib import Path

from app.compliance.reporter import ComplianceReporter
from app.compliance.runner import ComplianceRunner
from app.excel_data_handling.excel_data_handler import ExcelDataHandler
from app.logging.logger import setup_logging
from app.pipeline import run_pipeline
from app.printing.printer import Printer
from app.utils import db_session

setup_logging()

with db_session() as session:
    # To rebuild from an empty DB, uncomment for a single run, then re-comment:
    # ExcelDataHandler.wipe_db()
    run_pipeline(session=session)

# Device data is printed based on stored computed data in dB.
with db_session() as session:
    printer = Printer(session=session)

    config = printer.render_device(hostname="cedge4.tst-001")
    # print(config)

    # runner = ComplianceRunner(get_latest=False, session=session)
    # summary = runner.run()

    # reporter = ComplianceReporter(report_dir=Path("app/compliance/reports"))
    # reporter.report(summary=summary)
