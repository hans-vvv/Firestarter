from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from app.automation.backup import FetchResult, write_run
from app.compliance.differ import DiffResult
from app.compliance.normaliser import NormalisedConfig
from app.compliance.runner import ComplianceRunner, ComplianceRunSummary


@dataclass
class _FakeFetchResult:
    """Minimal stand-in for backup.py's FetchResult (raw + ok)."""

    raw: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------------------------------------------------------------------------
# ComplianceRunSummary — computed properties
# ---------------------------------------------------------------------------


def _diff(compliant: bool) -> DiffResult:
    lines = frozenset() if compliant else frozenset(["extra line"])
    return DiffResult(
        hostname="host",
        only_in_rendered=lines,
        only_in_live=frozenset(),
        ignored_rendered=0,
        ignored_live=0,
    )


class TestComplianceRunSummary:
    def test_compliant_count_counts_compliant_results(self):
        summary = ComplianceRunSummary(
            results={
                "h1": _diff(True),
                "h2": _diff(False),
                "h3": _diff(True),
            }
        )
        assert summary.compliant_count == 2

    def test_non_compliant_count_counts_non_compliant_results(self):
        summary = ComplianceRunSummary(
            results={
                "h1": _diff(True),
                "h2": _diff(False),
            }
        )
        assert summary.non_compliant_count == 1

    def test_total_attempted_sums_all_categories(self):
        summary = ComplianceRunSummary(
            results={"h1": _diff(True)},
            failed_render=["h2"],
            failed_fetch=["h3", "h4"],
        )
        assert summary.total_attempted == 4

    def test_empty_summary_all_zeros(self):
        summary = ComplianceRunSummary()
        assert summary.compliant_count == 0
        assert summary.non_compliant_count == 0
        assert summary.total_attempted == 0

    def test_all_compliant(self):
        summary = ComplianceRunSummary(results={"h1": _diff(True), "h2": _diff(True)})
        assert summary.compliant_count == 2
        assert summary.non_compliant_count == 0

    def test_all_non_compliant(self):
        summary = ComplianceRunSummary(results={"h1": _diff(False), "h2": _diff(False)})
        assert summary.compliant_count == 0
        assert summary.non_compliant_count == 2

    def test_total_attempted_excludes_skipped(self):
        summary = ComplianceRunSummary(
            results={"h1": _diff(True)},
            skipped=["h2", "h3"],
        )
        # skipped are not counted in total_attempted by design
        assert summary.total_attempted == 1

    def test_total_attempted_includes_failed_live(self):
        summary = ComplianceRunSummary(
            results={"h1": _diff(True)},
            failed_live=["h2", "h3"],
        )
        # failed_live attempts were made; they belong in total_attempted
        # so the rollup reflects the real fleet size.
        assert summary.total_attempted == 3


# ---------------------------------------------------------------------------
# ComplianceRunner — failure routing in the normalisation stages
# ---------------------------------------------------------------------------


class _StubNormaliser:
    """Stand-in for ComplianceNormaliser that raises for chosen hostnames.

    Used to drive the runner's per-device error handling without needing a
    real session, real ignore YAML, or real device records.
    """

    def __init__(self, *, raise_for: set[str]) -> None:
        self._raise_for = raise_for

    def _build(self, hostname: str) -> NormalisedConfig:
        if hostname in self._raise_for:
            raise RuntimeError(f"boom: {hostname}")
        return NormalisedConfig(
            hostname=hostname,
            lines=frozenset({"configure router bgp"}),
            ignored_count=0,
        )

    def normalise(self, *, hostname: str, raw: str) -> NormalisedConfig:
        return self._build(hostname)

    def normalise_rendered(self, *, hostname: str, raw: str) -> NormalisedConfig:
        return self._build(hostname)


class TestRunnerFailureRouting:
    def test_normalise_live_records_failed_hostnames(self, session):
        # Previously _normalise_live silently swallowed exceptions, causing the
        # device to vanish from the report.  It must now be recorded so the
        # dashboard can surface the error.
        runner = ComplianceRunner(session=session)
        runner._normaliser = _StubNormaliser(raise_for={"bad-host"})  # type: ignore[assignment]
        summary = ComplianceRunSummary()

        result = runner._normalise_live(
            raw_configs={"good-host": "...", "bad-host": "..."},
            summary=summary,
        )

        assert "good-host" in result
        assert "bad-host" not in result
        assert summary.failed_live == ["bad-host"]

    def test_normalise_rendered_records_failed_hostnames(self, session):
        runner = ComplianceRunner(session=session)
        runner._normaliser = _StubNormaliser(raise_for={"bad-host"})  # type: ignore[assignment]
        summary = ComplianceRunSummary()

        result = runner._normalise_rendered(
            raw_configs={"good-host": "...", "bad-host": "..."},
            summary=summary,
        )

        assert "good-host" in result
        assert "bad-host" not in result
        assert summary.failed_render == ["bad-host"]

    def test_normalise_live_isolates_per_device(self, session):
        # One bad device must not prevent the good ones from being normalised.
        runner = ComplianceRunner(session=session)
        runner._normaliser = _StubNormaliser(raise_for={"bad1", "bad2"})  # type: ignore[assignment]
        summary = ComplianceRunSummary()

        result = runner._normalise_live(
            raw_configs={"good1": "...", "bad1": "...", "good2": "...", "bad2": "..."},
            summary=summary,
        )

        assert set(result.keys()) == {"good1", "good2"}
        assert set(summary.failed_live) == {"bad1", "bad2"}


# ---------------------------------------------------------------------------
# ComplianceRunner._load_live — single source: the latest local backup run
# ---------------------------------------------------------------------------


class TestLoadLiveFromBackupStore:
    """The live side is now one store, written by app/automation/backup.py."""

    def _write_run(self, backups, results):
        write_run(results=results, timestamp="2026-07-21T09:44:53", backups_dir=backups)

    def test_reads_configs_from_the_latest_run(self, session, tmp_path):
        self._write_run(
            tmp_path,
            {
                "lab-r1": FetchResult(hostname="lab-r1", raw="configure system name lab-r1\n"),
                "lab-r2": FetchResult(hostname="lab-r2", raw="configure system name lab-r2\n"),
            },
        )
        runner = ComplianceRunner(session=session, backups_dir=tmp_path)
        summary = ComplianceRunSummary()

        live_raw = runner._load_live(summary=summary)

        assert live_raw == {
            "lab-r1": "configure system name lab-r1\n",
            "lab-r2": "configure system name lab-r2\n",
        }
        assert summary.failed_fetch == []

    def test_unreachable_device_is_reported_not_dropped(self, session, tmp_path):
        """ "Could not reach it" must stay distinguishable from "it has drifted"."""
        self._write_run(
            tmp_path,
            {
                "lab-r1": FetchResult(hostname="lab-r1", raw="configure system name lab-r1\n"),
                "lab-r2": FetchResult(hostname="lab-r2", raw=None, error="timeout"),
            },
        )
        runner = ComplianceRunner(session=session, backups_dir=tmp_path)
        summary = ComplianceRunSummary()

        live_raw = runner._load_live(summary=summary)

        assert live_raw == {"lab-r1": "configure system name lab-r1\n"}
        assert summary.failed_fetch == ["lab-r2"]

    def test_no_backup_run_yet_is_not_an_error(self, session, tmp_path):
        """A fresh checkout has nothing to compare against; that is legitimate."""
        runner = ComplianceRunner(session=session, backups_dir=tmp_path)
        summary = ComplianceRunSummary()

        assert runner._load_live(summary=summary) == {}
        assert summary.failed_fetch == []
