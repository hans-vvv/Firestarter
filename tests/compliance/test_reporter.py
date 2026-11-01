from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.compliance.differ import DiffResult
from app.compliance.reporter import ComplianceReporter
from app.compliance.runner import ComplianceRunSummary

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _compliant(hostname: str) -> DiffResult:
    return DiffResult(
        hostname=hostname,
        only_in_rendered=frozenset(),
        only_in_live=frozenset(),
        ignored_rendered=0,
        ignored_live=0,
    )


def _drifted(
    hostname: str,
    missing: set[str] | None = None,
    unexpected: set[str] | None = None,
    ignored_rendered: int = 0,
    ignored_live: int = 0,
) -> DiffResult:
    return DiffResult(
        hostname=hostname,
        only_in_rendered=frozenset(missing or set()),
        only_in_live=frozenset(unexpected or set()),
        ignored_rendered=ignored_rendered,
        ignored_live=ignored_live,
    )


def _summary(
    results: dict | None = None,
    failed_render: list[str] | None = None,
    failed_fetch: list[str] | None = None,
    skipped: list[str] | None = None,
) -> ComplianceRunSummary:
    s = ComplianceRunSummary()
    s.results = results or {}
    s.failed_render = failed_render or []
    s.failed_fetch = failed_fetch or []
    s.skipped = skipped or []
    return s


# ------------------------------------------------------------------
# ComplianceRunSummary properties
# ------------------------------------------------------------------


class TestComplianceRunSummary:
    def test_compliant_count(self):
        s = _summary(
            results={
                "r1": _compliant("r1"),
                "r2": _drifted("r2", missing={"x"}),
            }
        )
        assert s.compliant_count == 1

    def test_non_compliant_count(self):
        s = _summary(
            results={
                "r1": _compliant("r1"),
                "r2": _drifted("r2", missing={"x"}),
                "r3": _drifted("r3", unexpected={"y"}),
            }
        )
        assert s.non_compliant_count == 2

    def test_total_attempted_includes_failures(self):
        s = _summary(
            results={"r1": _compliant("r1")},
            failed_render=["r2"],
            failed_fetch=["r3"],
        )
        assert s.total_attempted == 3

    def test_all_compliant(self):
        s = _summary(results={"r1": _compliant("r1"), "r2": _compliant("r2")})
        assert s.compliant_count == 2
        assert s.non_compliant_count == 0


# ------------------------------------------------------------------
# Report text content
# ------------------------------------------------------------------


class TestComplianceReporterOutput:
    def setup_method(self):
        self.reporter = ComplianceReporter()

    def test_header_contains_timestamp(self):
        s = _summary(results={"r1": _compliant("r1")})
        text = self.reporter.report(summary=s)
        assert "COMPLIANCE REPORT" in text
        assert "UTC" in text

    def test_summary_table_shows_compliant_status(self):
        s = _summary(results={"r1": _compliant("r1")})
        text = self.reporter.report(summary=s)
        assert "COMPLIANT" in text
        assert "r1" in text

    def test_summary_table_shows_drift_status(self):
        s = _summary(results={"r1": _drifted("r1", missing={"configure router bgp"})})
        text = self.reporter.report(summary=s)
        assert "DRIFT" in text

    def test_summary_table_shows_totals_line(self):
        s = _summary(results={"r1": _compliant("r1"), "r2": _drifted("r2", missing={"x"})})
        text = self.reporter.report(summary=s)
        assert "1/2 devices compliant" in text

    def test_drift_detail_section_present_when_non_compliant(self):
        s = _summary(results={"r1": _drifted("r1", missing={"configure router bgp"})})
        text = self.reporter.report(summary=s)
        assert "DRIFT DETAIL" in text

    def test_drift_detail_section_absent_when_all_compliant(self):
        s = _summary(results={"r1": _compliant("r1")})
        text = self.reporter.report(summary=s)
        assert "DRIFT DETAIL" not in text

    def test_missing_lines_labelled_correctly(self):
        s = _summary(results={"r1": _drifted("r1", missing={"configure router bgp"})})
        text = self.reporter.report(summary=s)
        assert "MISSING" in text
        assert "configure router bgp" in text

    def test_unexpected_lines_labelled_correctly(self):
        s = _summary(results={"r1": _drifted("r1", unexpected={"stale-config"})})
        text = self.reporter.report(summary=s)
        assert "UNEXPECTED" in text
        assert "stale-config" in text

    def test_failures_section_present_when_render_failure(self):
        s = _summary(results={}, failed_render=["r2"])
        text = self.reporter.report(summary=s)
        assert "FAILURES" in text
        assert "r2" in text

    def test_failures_section_present_when_fetch_failure(self):
        s = _summary(results={}, failed_fetch=["r3"])
        text = self.reporter.report(summary=s)
        assert "FAILURES" in text
        assert "r3" in text

    def test_failures_section_absent_when_no_failures(self):
        s = _summary(results={"r1": _compliant("r1")})
        text = self.reporter.report(summary=s)
        assert "FAILURES" not in text

    def test_footer_contains_run_totals(self):
        s = _summary(
            results={"r1": _compliant("r1"), "r2": _drifted("r2", missing={"x"})},
            failed_render=["r3"],
        )
        text = self.reporter.report(summary=s)
        assert "attempted=3" in text
        assert "compliant=1" in text
        assert "drift=1" in text
        assert "render_fail=1" in text

    def test_ignored_counts_shown_as_ratio(self):
        s = _summary(
            results={"r1": _drifted("r1", missing={"x"}, ignored_rendered=5, ignored_live=3)}
        )
        text = self.reporter.report(summary=s)
        assert "5/3" in text

    def test_report_returns_string(self):
        s = _summary(results={"r1": _compliant("r1")})
        result = self.reporter.report(summary=s)
        assert isinstance(result, str)
        assert len(result) > 0


# ------------------------------------------------------------------
# File writing
# ------------------------------------------------------------------


class TestComplianceReporterFileOutput:
    def test_writes_file_to_report_dir(self, tmp_path):
        reporter = ComplianceReporter(report_dir=tmp_path)
        s = _summary(results={"r1": _compliant("r1")})
        reporter.report(summary=s)
        files = list(tmp_path.iterdir())
        assert len(files) == 1

    def test_filename_matches_timestamp_pattern(self, tmp_path):
        reporter = ComplianceReporter(report_dir=tmp_path)
        s = _summary(results={"r1": _compliant("r1")})
        reporter.report(summary=s)
        filename = next(iter(tmp_path.iterdir())).name
        assert re.match(r"compliance_\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.txt", filename)

    def test_file_content_matches_returned_string(self, tmp_path):
        reporter = ComplianceReporter(report_dir=tmp_path)
        s = _summary(results={"r1": _compliant("r1")})
        text = reporter.report(summary=s)
        written = next(iter(tmp_path.iterdir())).read_text(encoding="utf-8")
        assert text == written

    def test_creates_report_dir_if_missing(self, tmp_path):
        report_dir = tmp_path / "nested" / "reports"
        reporter = ComplianceReporter(report_dir=report_dir)
        s = _summary(results={"r1": _compliant("r1")})
        reporter.report(summary=s)
        assert report_dir.exists()

    def test_successive_runs_produce_separate_files(self, tmp_path):
        reporter = ComplianceReporter(report_dir=tmp_path)
        s = _summary(results={"r1": _compliant("r1")})
        reporter.report(summary=s)
        reporter.report(summary=s)
        assert len(list(tmp_path.iterdir())) >= 1

    def test_no_file_written_when_report_dir_is_none(self, tmp_path):
        reporter = ComplianceReporter(report_dir=None)
        s = _summary(results={"r1": _compliant("r1")})
        reporter.report(summary=s)
        assert list(tmp_path.iterdir()) == []
