"""Unit tests for :mod:`app.logging.logger` — the two log streams and the
compress-on-rotation ZIP retention of their daily files."""

from __future__ import annotations

import logging
import zipfile
from logging.handlers import TimedRotatingFileHandler

import pytest

import app.logging.logger as logger_mod


def _clear_handlers(name: str) -> logging.Logger:
    """Strip every handler from *name* and return it.

    Used right before ``setup_*`` in the handler-asserting tests: those installers
    early-return on ``if logger.handlers``, and pytest's own ``LogCaptureHandler``
    is attached to these loggers at call time (after fixture setup), which would
    otherwise suppress the real install.
    """
    lg = logging.getLogger(name)
    for handler in lg.handlers[:]:
        handler.close()
        lg.removeHandler(handler)
    return lg


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Redirect logging at a per-test tmp dir with the global loggers cleared."""
    monkeypatch.setenv("FIRESTARTER_LOG_DIR", str(tmp_path))
    for name in (logger_mod._ROOT, logger_mod.AUDIT_LOGGER):
        _clear_handlers(name)
    yield tmp_path
    for name in (logger_mod._ROOT, logger_mod.AUDIT_LOGGER):
        _clear_handlers(name)


def test_setup_logging_installs_file_and_stream(log_dir):
    app_log = _clear_handlers("app")
    logger_mod.setup_logging()

    # A plain StreamHandler (→ stderr → journal) AND a daily-rotating file
    # handler. FileHandler subclasses StreamHandler, so identify the plain one by
    # exact type.
    assert any(type(h) is logging.StreamHandler for h in app_log.handlers)
    assert any(isinstance(h, TimedRotatingFileHandler) for h in app_log.handlers)
    assert app_log.propagate is False

    app_log.info("diagnostic-line")
    assert "diagnostic-line" in (log_dir / "logging.log").read_text()


def test_setup_logging_is_idempotent(log_dir):
    _clear_handlers("app")
    logger_mod.setup_logging()
    logger_mod.setup_logging()
    assert len(logging.getLogger("app").handlers) == 2


def test_audit_logger_is_separate_and_writes(log_dir):
    audit = _clear_handlers("audit")
    logger_mod.setup_audit_logging()

    assert audit.propagate is False  # never leaks into the app/diagnostic stream
    logger_mod.get_audit_logger().info("user=bob event=test")

    assert "event=test" in (log_dir / "transactions.log").read_text()
    # And nothing landed in the diagnostic log.
    assert not (log_dir / "logging.log").exists()


def test_audit_setup_installs_rotating_handler(log_dir):
    audit = _clear_handlers("audit")
    logger_mod.setup_audit_logging()
    assert any(isinstance(h, TimedRotatingFileHandler) for h in audit.handlers)


def test_rotation_zips_the_daily_into_the_subdir(log_dir):
    """A live rollover compresses the daily into transactions_zipped/ — never loose."""
    _clear_handlers("audit")
    logger_mod.setup_audit_logging()
    logger_mod.get_audit_logger().info("user=bob event=first")

    handler = next(
        h for h in logging.getLogger("audit").handlers if isinstance(h, TimedRotatingFileHandler)
    )
    handler.doRollover()

    # The live file is fresh again and no loose daily was left beside it.
    loose = list(log_dir.glob("transactions.log.*"))
    assert loose == []

    zipped = list((log_dir / "transactions_zipped").glob("*.zip"))
    assert len(zipped) == 1
    with zipfile.ZipFile(zipped[0]) as zf:
        (name,) = zf.namelist()
        assert name.startswith("transactions.log.")  # <base>.<date>
        assert b"event=first" in zf.read(name)


def test_startup_sweep_zips_loose_dailies(log_dir):
    """A daily left loose by an earlier build is folded into the zip subdir."""
    loose = log_dir / "transactions.log.2020-01-01"
    loose.write_text("old-day")

    created = logger_mod.archive_old_transaction_logs()

    assert not loose.exists()
    zip_path = log_dir / "transactions_zipped" / "transactions.log.2020-01-01.zip"
    assert created == [zip_path]
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read("transactions.log.2020-01-01") == b"old-day"


def test_startup_sweep_ignores_non_daily_names(log_dir):
    (log_dir / "transactions.log").write_text("live")  # the active file
    (log_dir / "transactions.log.bak").write_text("not a date")

    assert logger_mod.archive_old_transaction_logs() == []
    assert (log_dir / "transactions.log").exists()
    assert (log_dir / "transactions.log.bak").exists()


def test_application_log_sweep_uses_its_own_subdir(log_dir):
    (log_dir / "logging.log.2020-02-02").write_text("app-day")

    created = logger_mod.archive_old_application_logs()

    zip_path = log_dir / "logging_zipped" / "logging.log.2020-02-02.zip"
    assert created == [zip_path]
    assert not (log_dir / "logging.log.2020-02-02").exists()


class TestPruneZipDir:
    """The zip subdir is capped at the newest ``keep`` archives."""

    def _make_zips(self, directory, dates):
        directory.mkdir(parents=True, exist_ok=True)
        for d in dates:
            (directory / f"transactions.log.{d}.zip").write_bytes(b"z")

    def test_noop_at_or_under_limit(self, tmp_path):
        d = tmp_path / "z"
        dates = ["2026-01-01", "2026-01-02", "2026-01-03"]
        self._make_zips(d, dates)
        assert logger_mod._prune_zip_dir(d, keep=3) == []
        assert len(list(d.glob("*.zip"))) == 3

    def test_removes_oldest_beyond_keep(self, tmp_path):
        d = tmp_path / "z"
        dates = [f"2026-01-{day:02d}" for day in range(1, 11)]  # 10 archives
        self._make_zips(d, dates)
        removed = logger_mod._prune_zip_dir(d, keep=3)

        assert [p.name for p in removed] == [f"transactions.log.{x}.zip" for x in dates[:7]]
        remaining = sorted(p.name for p in d.glob("*.zip"))
        assert remaining == [f"transactions.log.{x}.zip" for x in dates[7:]]

    def test_sweep_prunes_to_retention(self, log_dir, monkeypatch):
        monkeypatch.setattr(logger_mod, "AUDIT_RETENTION_ZIPS", 2)
        for day in ("2020-01-01", "2020-01-02", "2020-01-03"):
            (log_dir / f"transactions.log.{day}").write_text(day)

        logger_mod.archive_old_transaction_logs()

        kept = sorted(p.name for p in (log_dir / "transactions_zipped").glob("*.zip"))
        assert kept == [
            "transactions.log.2020-01-02.zip",
            "transactions.log.2020-01-03.zip",
        ]
