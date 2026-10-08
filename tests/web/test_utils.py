from __future__ import annotations

"""Unit tests for app.web.utils — pure functions and thin facades (no HTTP)."""

import contextlib

import pytest

import app.web.utils as web_utils
from app.automation.backup import FetchResult
from app.compliance.runner import ComplianceRunSummary
from app.web.utils import (
    compliance_report_text,
    filter_devices,
    get_last_compliance_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEVICES = [
    {
        "id": 1,
        "hostname": "pe1.ams",
        "role": "pe",
        "site": "ams-01",
        "model_name": "7750-SR-7",
        "status": "active",
        "lag_name": None,
        "labels": {},
    },
    {
        "id": 2,
        "hostname": "pe2.ams",
        "role": "pe",
        "site": "ams-01",
        "model_name": "7750-SR-7",
        "status": "active",
        "lag_name": None,
        "labels": {},
    },
    {
        "id": 3,
        "hostname": "p1.fra",
        "role": "p",
        "site": "fra-01",
        "model_name": "7750-SR-12",
        "status": "active",
        "lag_name": None,
        "labels": {},
    },
    {
        "id": 4,
        "hostname": "ce1.ams",
        "role": "ce",
        "site": "ams-01",
        "model_name": "7210-SAS-M",
        "status": "staged",
        "lag_name": None,
        "labels": {},
    },
]


# ---------------------------------------------------------------------------
# filter_devices
# ---------------------------------------------------------------------------


class TestFilterDevices:
    def test_no_filters_returns_all(self):
        assert filter_devices(_DEVICES) == _DEVICES

    def test_hostname_substring_match(self):
        result = filter_devices(_DEVICES, hostname="pe")
        assert [d["hostname"] for d in result] == ["pe1.ams", "pe2.ams"]

    def test_hostname_case_insensitive(self):
        result = filter_devices(_DEVICES, hostname="PE1")
        assert len(result) == 1
        assert result[0]["hostname"] == "pe1.ams"

    def test_hostname_no_match_returns_empty(self):
        assert filter_devices(_DEVICES, hostname="zzz") == []

    def test_role_exact_match(self):
        result = filter_devices(_DEVICES, role="pe")
        assert all(d["role"] == "pe" for d in result)
        assert len(result) == 2

    def test_site_exact_match(self):
        result = filter_devices(_DEVICES, site="fra-01")
        assert len(result) == 1
        assert result[0]["hostname"] == "p1.fra"

    def test_model_exact_match(self):
        result = filter_devices(_DEVICES, model="7210-SAS-M")
        assert len(result) == 1
        assert result[0]["hostname"] == "ce1.ams"

    def test_status_exact_match(self):
        result = filter_devices(_DEVICES, status="staged")
        assert len(result) == 1
        assert result[0]["status"] == "staged"

    def test_combined_filters_are_anded(self):
        result = filter_devices(_DEVICES, role="pe", site="ams-01")
        assert len(result) == 2
        assert all(d["role"] == "pe" and d["site"] == "ams-01" for d in result)

    def test_combined_filters_no_match(self):
        result = filter_devices(_DEVICES, role="pe", site="fra-01")
        assert result == []


# ---------------------------------------------------------------------------
# Compliance run cache + report formatter
# ---------------------------------------------------------------------------


class TestComplianceReportCache:
    """The detailed-report download formats the last cached run in-process.

    These poke the module-level cache directly (no DB) so the formatter and
    its empty-cache guard are covered without running the pipeline.
    """

    def test_report_text_is_none_when_never_run(self, monkeypatch):
        monkeypatch.setattr(web_utils, "_compliance_last_summary", None)
        assert compliance_report_text() is None
        assert get_last_compliance_summary() is None

    def test_report_text_formats_cached_summary(self, monkeypatch):
        summary = ComplianceRunSummary()  # empty run: all sections still render
        monkeypatch.setattr(web_utils, "_compliance_last_summary", summary)

        assert get_last_compliance_summary() is summary

        text = compliance_report_text()
        assert text is not None
        assert "COMPLIANCE REPORT" in text
        assert "0/0 devices compliant" in text


# ---------------------------------------------------------------------------
# download_device_backups — the simulated-device facade
# ---------------------------------------------------------------------------


class TestDownloadDeviceBackups:
    @staticmethod
    def _patch(monkeypatch, results):
        calls = []

        def _run(*, session):
            calls.append(session)
            return results

        monkeypatch.setattr(web_utils, "run_simulated_backup", _run)
        monkeypatch.setattr(web_utils, "db_session", lambda: contextlib.nullcontext("fake-session"))
        return calls

    def test_runs_the_simulation_without_credentials(self, monkeypatch):
        calls = self._patch(
            monkeypatch,
            {
                "pe1": FetchResult(hostname="pe1", raw="configure a\n"),
                "pe2": FetchResult(hostname="pe2", raw="configure b\n"),
            },
        )
        outcome = web_utils.download_device_backups()
        assert calls == ["fake-session"]
        assert outcome.ok
        assert outcome.name == "Fetch backups from simulated devices"
        assert outcome.detail == "2/2 simulated device(s) fetched"

    def test_unreachable_device_is_reported_not_fatal(self, monkeypatch):
        self._patch(
            monkeypatch,
            {
                "pe1": FetchResult(hostname="pe1", raw="configure a\n"),
                "pe9": FetchResult(hostname="pe9", raw=None, error="TCP connection failed."),
            },
        )
        outcome = web_utils.download_device_backups()
        assert not outcome.ok
        assert outcome.detail == "1/2 simulated device(s) fetched — unreachable: pe9"

    def test_exception_becomes_a_failed_outcome(self, monkeypatch):
        def _boom(*, session):
            raise RuntimeError("no configs rendered")

        monkeypatch.setattr(web_utils, "run_simulated_backup", _boom)
        monkeypatch.setattr(web_utils, "db_session", lambda: contextlib.nullcontext(None))
        outcome = web_utils.download_device_backups()
        assert not outcome.ok
        assert "no configs rendered" in outcome.detail
