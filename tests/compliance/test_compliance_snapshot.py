from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.compliance.compliance_snapshot import BASELINE, REPORT_DIR, _lines_without_timestamp

# ---------------------------------------------------------------------------
# _lines_without_timestamp
# ---------------------------------------------------------------------------


class TestLinesWithoutTimestamp:
    def test_strips_timestamp_line(self):
        text = (
            "========================================================================\n"
            "  COMPLIANCE REPORT — 2026-05-23 03:43:00 UTC\n"
            "========================================================================\n"
            "SUMMARY\n"
        )
        result = _lines_without_timestamp(text)
        assert not any("COMPLIANCE REPORT —" in line for line in result)

    def test_preserves_non_timestamp_lines(self):
        text = "SUMMARY\nhost1  COMPLIANT\nhost2  DRIFT\n"
        result = _lines_without_timestamp(text)
        assert result == ["SUMMARY\n", "host1  COMPLIANT\n", "host2  DRIFT\n"]

    def test_empty_string_returns_empty_list(self):
        assert _lines_without_timestamp("") == []

    def test_keeps_lines_that_mention_report_in_other_context(self):
        # Only the exact header marker is stripped, not arbitrary mentions
        text = "  Render failures (config could not be generated):\n"
        result = _lines_without_timestamp(text)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Baseline save / diff — main() logic tested via its helpers
# ---------------------------------------------------------------------------


class TestBaselineSave:
    def test_baseline_written_to_disk(self, tmp_path, monkeypatch):
        baseline = tmp_path / "sprint_baseline.txt"
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        fake_report = "SUMMARY\nhost1  COMPLIANT\n"
        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value=fake_report),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
            patch("app.compliance.compliance_snapshot._print"),
        ):
            from app.compliance.compliance_snapshot import main

            with patch("sys.argv", ["compliance_snapshot.py"]):
                main()

        assert baseline.exists()
        assert baseline.read_text(encoding="utf-8") == fake_report

    def test_baseline_overwritten_on_second_run(self, tmp_path, monkeypatch):
        baseline = tmp_path / "sprint_baseline.txt"
        baseline.write_text("old content", encoding="utf-8")
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        new_report = "SUMMARY\nhost1  DRIFT\n"
        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value=new_report),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
            patch("app.compliance.compliance_snapshot._print"),
        ):
            from app.compliance.compliance_snapshot import main

            with patch("sys.argv", ["compliance_snapshot.py"]):
                main()

        assert baseline.read_text(encoding="utf-8") == new_report


class TestDiffMode:
    def _write_baseline(self, tmp_path: Path, content: str) -> Path:
        baseline = tmp_path / "sprint_baseline.txt"
        baseline.write_text(content, encoding="utf-8")
        return baseline

    def test_no_changes_message_when_reports_identical(self, tmp_path, monkeypatch, capsys):
        report = "SUMMARY\nhost1  COMPLIANT\n"
        baseline = self._write_baseline(tmp_path, report)
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        printed: list[str] = []
        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value=report),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
            patch("app.compliance.compliance_snapshot._print", side_effect=printed.append),
        ):
            from app.compliance.compliance_snapshot import main

            with patch("sys.argv", ["compliance_snapshot.py", "--diff"]):
                main()

        assert any("No changes" in msg for msg in printed)

    def test_diff_shown_when_report_changed(self, tmp_path, monkeypatch):
        baseline_text = "SUMMARY\nhost1  COMPLIANT\n"
        new_text = "SUMMARY\nhost1  DRIFT\n"
        baseline = self._write_baseline(tmp_path, baseline_text)
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        printed: list[str] = []
        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value=new_text),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
            patch("app.compliance.compliance_snapshot._print", side_effect=printed.append),
        ):
            from app.compliance.compliance_snapshot import main

            with patch("sys.argv", ["compliance_snapshot.py", "--diff"]):
                main()

        full_output = "".join(printed)
        assert "COMPLIANT" in full_output or "DRIFT" in full_output

    def test_end_of_sprint_report_saved(self, tmp_path, monkeypatch):
        report = "SUMMARY\nhost1  COMPLIANT\n"
        baseline = self._write_baseline(tmp_path, report)
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value=report),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
            patch("app.compliance.compliance_snapshot._print"),
        ):
            from app.compliance.compliance_snapshot import main

            with patch("sys.argv", ["compliance_snapshot.py", "--diff"]):
                main()

        saved = list(tmp_path.glob("sprint_end_*.txt"))
        assert len(saved) == 1
        assert saved[0].read_text(encoding="utf-8") == report

    def test_exits_with_error_when_no_baseline(self, tmp_path, monkeypatch):
        baseline = tmp_path / "sprint_baseline.txt"  # does not exist
        monkeypatch.setattr("app.compliance.compliance_snapshot.REPORT_DIR", tmp_path)
        monkeypatch.setattr("app.compliance.compliance_snapshot.BASELINE", baseline)

        with (
            patch("app.compliance.compliance_snapshot._run_and_report", return_value="report"),
            patch("app.compliance.compliance_snapshot._ensure_repo_root"),
        ):
            from app.compliance.compliance_snapshot import main

            with (
                patch("sys.argv", ["compliance_snapshot.py", "--diff"]),
                pytest.raises(SystemExit),
            ):
                main()
