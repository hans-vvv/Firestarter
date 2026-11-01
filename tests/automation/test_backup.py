"""Tests for the Nornir config-backup store.

Everything here runs against ``tmp_path``; the one function that needs devices
(``run_backup``) is not exercised, but the archive/write/read logic it depends on
is, which is where the data-loss risks live.
"""

from __future__ import annotations

import json

import pytest

from app.automation.backup import (
    FetchResult,
    archive_latest,
    latest_dir,
    load_results,
    prune_old_backups,
    read_backup,
    read_timestamp,
    to_fetch_results,
    write_run,
)
from app.automation.commands import CommandResult

TS = "2026-07-21T09:44:53"


def _write_latest(backups, *, timestamp=TS, configs=None):
    latest = backups / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    (latest / "datetime.txt").write_text(timestamp + "\n", encoding="utf-8")
    for name, text in (configs or {}).items():
        (latest / f"{name}.cfg").write_text(text, encoding="utf-8")
    return latest


class TestArchiveLatest:
    def test_no_latest_is_a_noop(self, tmp_path):
        assert archive_latest(backups_dir=tmp_path) is None

    def test_archive_is_named_for_the_data_not_the_run(self, tmp_path):
        """The label must reflect when the configs were collected."""
        _write_latest(tmp_path, timestamp="2026-01-02T03:04:05")
        target = archive_latest(backups_dir=tmp_path)

        assert target is not None
        assert target.name == "2026-01-02T03-04-05"
        assert not latest_dir(backups_dir=tmp_path).exists()

    def test_contents_survive_archiving(self, tmp_path):
        _write_latest(tmp_path, configs={"core2.tst-001": "config A\n"})
        target = archive_latest(backups_dir=tmp_path)
        assert (target / "core2.tst-001.cfg").read_text() == "config A\n"

    def test_missing_timestamp_falls_back_to_mtime(self, tmp_path):
        latest = tmp_path / "latest"
        latest.mkdir(parents=True)
        target = archive_latest(backups_dir=tmp_path)
        assert target is not None
        assert target.exists()

    def test_two_runs_in_the_same_second_do_not_collide(self, tmp_path):
        _write_latest(tmp_path)
        first = archive_latest(backups_dir=tmp_path)
        _write_latest(tmp_path)
        second = archive_latest(backups_dir=tmp_path)

        assert first != second
        assert first.exists()
        assert second.exists()


class TestWriteRun:
    def test_writes_one_cfg_per_successful_device(self, tmp_path):
        results = {
            "core2.tst-001": FetchResult(hostname="core2.tst-001", raw="config A"),
            "core3.tst-001": FetchResult(hostname="core3.tst-001", raw="config B"),
        }
        latest = write_run(results=results, timestamp=TS, backups_dir=tmp_path)

        assert (latest / "core2.tst-001.cfg").read_text() == "config A\n"
        assert (latest / "core3.tst-001.cfg").read_text() == "config B\n"

    def test_failed_device_gets_no_cfg_file(self, tmp_path):
        """A missing config must never be mistaken for a real one."""
        results = {
            "rr1.tst-001": FetchResult(hostname="rr1.tst-001", raw=None, error="timeout"),
        }
        latest = write_run(results=results, timestamp=TS, backups_dir=tmp_path)

        assert not (latest / "rr1.tst-001.cfg").exists()
        payload = json.loads((latest / "fetch_results.json").read_text())
        assert payload["results"]["rr1.tst-001"] == {"ok": False, "error": "timeout"}

    def test_trailing_newline_is_normalised(self, tmp_path):
        results = {"a": FetchResult(hostname="a", raw="line\n")}
        latest = write_run(results=results, timestamp=TS, backups_dir=tmp_path)
        assert (latest / "a.cfg").read_text() == "line\n"

    def test_timestamp_is_recorded(self, tmp_path):
        latest = write_run(results={}, timestamp=TS, backups_dir=tmp_path)
        assert (latest / "datetime.txt").read_text().strip() == TS

    def test_configs_are_not_duplicated_into_the_json(self, tmp_path):
        """The .cfg files are the configs; the JSON carries outcomes only."""
        results = {"a": FetchResult(hostname="a", raw="secret-config-body")}
        latest = write_run(results=results, timestamp=TS, backups_dir=tmp_path)
        assert "secret-config-body" not in (latest / "fetch_results.json").read_text()


class TestLoadResults:
    def test_absent_run_returns_empty(self, tmp_path):
        """A fresh checkout has no backups; that is not an error."""
        assert load_results(backups_dir=tmp_path) == {}

    def test_round_trip_preserves_raw_and_error(self, tmp_path):
        written = {
            "core2.tst-001": FetchResult(hostname="core2.tst-001", raw="config A"),
            "rr1.tst-001": FetchResult(hostname="rr1.tst-001", raw=None, error="timeout"),
        }
        write_run(results=written, timestamp=TS, backups_dir=tmp_path)
        read_back = load_results(backups_dir=tmp_path)

        assert read_back["core2.tst-001"].raw == "config A\n"
        assert read_back["core2.tst-001"].ok is True
        assert read_back["rr1.tst-001"].raw is None
        assert read_back["rr1.tst-001"].ok is False
        assert read_back["rr1.tst-001"].error == "timeout"

    def test_no_pickle_involved(self, tmp_path):
        """Regression guard: the old pickle needed a __main__ class-identity hack."""
        write_run(
            results={"a": FetchResult(hostname="a", raw="x")}, timestamp=TS, backups_dir=tmp_path
        )
        assert not list((tmp_path / "latest").glob("*.pkl"))
        json.loads((tmp_path / "latest" / "fetch_results.json").read_text())


class TestToFetchResults:
    """The seam between the general command runner and the backup store.

    Turning Nornir's aggregated output into per-host results is now
    ``commands.collect_command_results``; what remains here is the narrowing of
    a one-command run down to the shape ``fetch_results.json`` persists.
    """

    def test_successful_host_carries_raw(self):
        collected = to_fetch_results(
            {"core1": [CommandResult(hostname="core1", command="show", output="config A")]}
        )
        assert collected["core1"].raw == "config A"
        assert collected["core1"].ok is True

    def test_failed_host_records_the_error(self):
        collected = to_fetch_results(
            {"rr1": [CommandResult(hostname="rr1", command="show", error="boom")]}
        )
        assert collected["rr1"].ok is False
        assert "boom" in collected["rr1"].error
        assert collected["rr1"].raw is None

    def test_one_failure_does_not_lose_the_others(self):
        collected = to_fetch_results(
            {
                "ok1": [CommandResult(hostname="ok1", command="show", output="A")],
                "bad": [CommandResult(hostname="bad", command="show", error="boom")],
                "ok2": [CommandResult(hostname="ok2", command="show", output="B")],
            }
        )
        assert sorted(collected) == ["bad", "ok1", "ok2"]
        assert collected["ok1"].raw == "A"
        assert collected["ok2"].raw == "B"


class TestPruneOldBackups:
    """Retention: keep only the newest ``keep`` archived runs; ``latest/`` is exempt."""

    def _make_archives(self, backups, names):
        for name in names:
            d = backups / name
            d.mkdir(parents=True)
            (d / "core2.tst-001.cfg").write_text("config\n", encoding="utf-8")

    def test_noop_when_at_or_under_limit(self, tmp_path):
        names = [f"2026-08-{d:02d}T00-00-00" for d in range(1, 4)]
        self._make_archives(tmp_path, names)
        removed = prune_old_backups(backups_dir=tmp_path, keep=3)
        assert removed == []
        assert sorted(p.name for p in tmp_path.iterdir()) == names

    def test_removes_only_the_oldest_beyond_keep(self, tmp_path):
        # Ten archives, keep 3 → the seven oldest go, the three newest stay.
        names = [f"2026-08-{d:02d}T00-00-00" for d in range(1, 11)]
        self._make_archives(tmp_path, names)
        removed = prune_old_backups(backups_dir=tmp_path, keep=3)

        assert sorted(p.name for p in removed) == names[:7]
        assert sorted(p.name for p in tmp_path.iterdir()) == names[7:]

    def test_latest_is_never_pruned(self, tmp_path):
        _write_latest(tmp_path)
        names = [f"2026-08-{d:02d}T00-00-00" for d in range(1, 6)]
        self._make_archives(tmp_path, names)
        prune_old_backups(backups_dir=tmp_path, keep=2)

        assert latest_dir(backups_dir=tmp_path).exists()
        remaining = sorted(p.name for p in tmp_path.iterdir() if p.name != "latest")
        assert remaining == names[3:]  # newest 2 archives kept, latest untouched

    def test_missing_dir_is_a_noop(self, tmp_path):
        assert prune_old_backups(backups_dir=tmp_path / "absent", keep=2) == []

    def test_stray_files_are_ignored(self, tmp_path):
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        names = [f"2026-08-{d:02d}T00-00-00" for d in range(1, 4)]
        self._make_archives(tmp_path, names)
        removed = prune_old_backups(backups_dir=tmp_path, keep=1)

        assert sorted(p.name for p in removed) == names[:2]
        assert (tmp_path / "notes.txt").exists()


class TestReadBackup:
    """Single-host read for the Latest backups config panel."""

    def _run(self, tmp_path):
        write_run(
            results={
                "core2.tst-001": FetchResult(hostname="core2.tst-001", raw="config A"),
                "rr1.tst-001": FetchResult(hostname="rr1.tst-001", raw=None, error="timeout"),
            },
            timestamp=TS,
            backups_dir=tmp_path,
        )

    def test_ok_device_carries_raw(self, tmp_path):
        self._run(tmp_path)
        r = read_backup("core2.tst-001", backups_dir=tmp_path)
        assert r is not None
        assert r.ok is True
        assert r.raw == "config A\n"

    def test_failed_device_has_error_and_no_raw(self, tmp_path):
        self._run(tmp_path)
        r = read_backup("rr1.tst-001", backups_dir=tmp_path)
        assert r is not None
        assert r.ok is False
        assert r.raw is None
        assert r.error == "timeout"

    def test_unknown_host_returns_none(self, tmp_path):
        self._run(tmp_path)
        assert read_backup("nope.tst-001", backups_dir=tmp_path) is None

    def test_no_run_returns_none(self, tmp_path):
        assert read_backup("core2.tst-001", backups_dir=tmp_path) is None

    def test_traversal_hostname_is_not_a_recorded_key(self, tmp_path):
        # A URL-supplied hostname that is not a key in the run's JSON never
        # reaches a file path — the key lookup returns None first.
        self._run(tmp_path)
        assert read_backup("../../etc/passwd", backups_dir=tmp_path) is None


class TestReadTimestamp:
    def test_returns_recorded_stamp(self, tmp_path):
        write_run(results={}, timestamp=TS, backups_dir=tmp_path)
        assert read_timestamp(backups_dir=tmp_path) == TS

    def test_none_when_no_run(self, tmp_path):
        assert read_timestamp(backups_dir=tmp_path) is None


class TestFetchResult:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [(None, True), ("timeout", False), ("", False)],
    )
    def test_ok_is_derived_from_error(self, error, expected):
        # Note the empty-string case: `ok` tests `is None`, not truthiness, so an
        # empty error message still marks the fetch as failed.
        assert FetchResult(hostname="h", raw="x", error=error).ok is expected
