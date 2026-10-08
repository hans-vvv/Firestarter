"""Sprint compliance regression guard.

Captures a compliance report as a baseline before a sprint, then diffs
against that baseline just before the final commit.  The live side is
always the most recent ``fetch_results.pkl`` — the pickle is held
constant across the sprint so the diff reflects only template and
handler changes, not device state changes.

Usage (invoke from any directory):
-----------------------------------
Before a sprint (save baseline):
    python app/compliance/compliance_snapshot.py

Before the final commit (diff against baseline):
    python app/compliance/compliance_snapshot.py --diff
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.domain.file_locations import COMPLIANCE_REPORTS_LOC

# Repo root is two levels above this file (app/compliance/compliance_snapshot.py).
# Still used to chdir so CWD-relative lookups (e.g. the Jinja2 template dir) and
# sys.path resolve, independent of where the reports now live.
_REPO_ROOT = Path(__file__).resolve().parents[2]

REPORT_DIR = COMPLIANCE_REPORTS_LOC.path
BASELINE = REPORT_DIR / "sprint_baseline.txt"


def _ensure_repo_root() -> None:
    """Add repo root to sys.path and chdir there.

    Called lazily from main() and _run_and_report() so that importing
    this module in tests does not trigger chdir or sys.path side-effects.
    """
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    os.chdir(_REPO_ROOT)


def _print(text: str) -> None:
    """Write UTF-8 text to stdout without hitting Windows cp1252 limits."""
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _run_and_report() -> str:
    _ensure_repo_root()
    from app.compliance.reporter import ComplianceReporter
    from app.compliance.runner import ComplianceRunner
    from app.utils import db_session

    with db_session() as session:
        runner = ComplianceRunner(session=session)
        summary = runner.run()
    return ComplianceReporter().report(summary=summary)


def _lines_without_timestamp(text: str) -> list[str]:
    """Return report lines with the run-timestamp header stripped.

    The timestamp line differs between every run by design, so keeping it
    would produce a meaningless diff entry on every comparison.
    """
    return [line for line in text.splitlines(keepends=True) if "COMPLIANCE REPORT —" not in line]


def main() -> None:
    _ensure_repo_root()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Diff the current run against the saved baseline.",
    )
    args = parser.parse_args()

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = _run_and_report()

    if not args.diff:
        BASELINE.write_text(report, encoding="utf-8")
        _print(report)
        _print(f"\nBaseline saved → {BASELINE}")
        return

    if not BASELINE.exists():
        sys.exit(
            f"No baseline found at {BASELINE}.\nRun without --diff first to capture a baseline."
        )

    baseline_text = BASELINE.read_text(encoding="utf-8")

    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    end_path = REPORT_DIR / f"sprint_end_{ts}.txt"
    end_path.write_text(report, encoding="utf-8")

    diff = list(
        difflib.unified_diff(
            _lines_without_timestamp(baseline_text),
            _lines_without_timestamp(report),
            fromfile="baseline",
            tofile="sprint-end",
        )
    )

    if not diff:
        _print("No changes — rendered configs are identical to baseline.")
    else:
        _print("".join(diff))

    _print(f"\nEnd-of-sprint report saved → {end_path}")


if __name__ == "__main__":
    main()
