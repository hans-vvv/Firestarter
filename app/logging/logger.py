"""Application logging setup.

Two independent streams, deliberately kept apart:

* the **application log** (``logging.log``) — INFO+ diagnostics and tracebacks for
  developers. It goes to a file *and* to stderr, so on the deployed dashboard the
  app's own warnings/exceptions still surface in ``journalctl`` alongside
  gunicorn's access/error output.
* the **transaction (audit) log** (``transactions.log``) — a structured record of
  *who did what* through the GUI, on its own ``audit`` logger with
  ``propagate=False`` so it never mixes into the diagnostic stream.

Both files rotate daily. Instead of leaving the rotated ``*.log.<date>`` dailies
loose in the log directory, each is compressed into its own ``.zip`` inside a
per-stream sub-folder (``transactions_zipped`` / ``logging_zipped``) at rotation
time, and each sub-folder is pruned to the newest :data:`AUDIT_RETENTION_ZIPS` /
:data:`APP_LOG_RETENTION_ZIPS` archives — so the log directory stays tidy and the
compressed history is bounded rather than unbounded. A startup sweep also folds
in any dailies left loose by an earlier build.

Paths default to ``logs/`` under the data root (overridable via
``FIRESTARTER_LOG_DIR``) rather than the process CWD, so logging works the same
whether started from a shell, ``python main.py``, or gunicorn.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def _log_dir() -> Path:
    """Directory both log files live in, resolved at call time.

    Defaults to ``logs/`` under the data root (so logs are a writable output beside
    the rest of the data, and the code tree / image stays clean) but is overridable
    via ``FIRESTARTER_LOG_DIR`` — set absolute in production if you want logs
    elsewhere, and pointed at a tmp dir by the test suite so tests never write into
    the repo tree. Resolved per call (not at import) so the override takes effect
    regardless of import order.
    """
    from app.domain.file_locations import LOGS_LOC

    env = os.getenv("FIRESTARTER_LOG_DIR")
    return Path(env) if env else LOGS_LOC.path


_FMT = "%(asctime)s [%(levelname)s] %(message)s"
_AUDIT_FMT = "%(asctime)s [AUDIT] %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_ROOT = "app"
AUDIT_LOGGER = "audit"

# TimedRotatingFileHandler(when="midnight") names rotated files
# ``<base>.log.YYYY-MM-DD``. Each is zipped into the sub-folder below and the
# sub-folder capped at this many archives (older ones deleted).
AUDIT_RETENTION_ZIPS = 90
APP_LOG_RETENTION_ZIPS = 90
_TX_ZIP_DIR = "transactions_zipped"
_APP_ZIP_DIR = "logging_zipped"


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the 'app' namespace."""
    return logging.getLogger(name)


def get_audit_logger() -> logging.Logger:
    """Return the dedicated transaction/audit logger."""
    return logging.getLogger(AUDIT_LOGGER)


def _prune_zip_dir(directory: Path, *, keep: int) -> list[Path]:
    """Delete ``*.zip`` archives in *directory* beyond the newest *keep*.

    Archive names are ``<base>.log.YYYY-MM-DD.zip``, which sort
    lexicographically in date order, so the newest *keep* are the tail of the
    sorted list and everything before it is old enough to drop. Returns the
    removed paths; a no-op when at or under the limit.
    """
    zips = sorted(directory.glob("*.zip"))
    if len(zips) <= keep:
        return []
    stale = zips[: len(zips) - keep]
    for path in stale:
        path.unlink()
    return stale


def _make_compressing_rotator(*, subdir: str, keep: int):
    """Build ``(namer, rotator)`` that zip each rolled daily into *subdir*.

    ``TimedRotatingFileHandler`` calls ``namer`` to pick the rotated file's name
    and ``rotator`` to move the closed log there. Together they redirect the
    dated daily into ``<subdir>/<name>.zip`` (compressed, plain file removed) and
    prune the sub-folder to the newest *keep* archives — so no uncompressed
    daily is ever left beside the live log.
    """

    def namer(default_name: str) -> str:
        src = Path(default_name)  # e.g. .../transactions.log.2026-09-21
        return str(src.parent / subdir / f"{src.name}.zip")

    def rotator(source: str, dest: str) -> None:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        # dest is ``.../<name>.zip``; store the daily under its plain name inside.
        with zipfile.ZipFile(dest_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(source, arcname=dest_path.stem)
        os.remove(source)
        _prune_zip_dir(dest_path.parent, keep=keep)

    return namer, rotator


def _install_daily_zip_rotation(
    handler: TimedRotatingFileHandler, *, subdir: str, keep: int
) -> None:
    """Wire compress-on-rotation into *handler* (see :func:`_make_compressing_rotator`)."""
    handler.namer, handler.rotator = _make_compressing_rotator(subdir=subdir, keep=keep)


def _sweep_loose_dailies(*, base_name: str, subdir: str, keep: int) -> list[Path]:
    """Zip any loose ``<base_name>.YYYY-MM-DD`` dailies into *subdir* and prune.

    Live rotation compresses dailies as they roll, but a daily can be left loose
    by an earlier build (or a crash between rename and compress). This sweeps
    those into ``<subdir>/<name>.zip``, removes the loose file, then caps the
    sub-folder at the newest *keep* archives. Idempotent and best-effort:
    anything not matching the ``<base>.log.<date>`` shape is skipped. Returns the
    zip paths created.
    """
    log_dir = _log_dir()
    if not log_dir.is_dir():
        return []

    dest = log_dir / subdir
    dest.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(base_name)}\.\d{{4}}-\d{{2}}-\d{{2}}$")

    created: list[Path] = []
    for path in sorted(log_dir.glob(f"{base_name}.*")):
        if not pattern.match(path.name):
            continue
        zip_path = dest / f"{path.name}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, arcname=path.name)
        path.unlink()
        created.append(zip_path)

    _prune_zip_dir(dest, keep=keep)
    return created


def archive_old_transaction_logs() -> list[Path]:
    """Sweep loose transaction dailies into :data:`_TX_ZIP_DIR` and prune. See
    :func:`_sweep_loose_dailies`."""
    return _sweep_loose_dailies(
        base_name="transactions.log", subdir=_TX_ZIP_DIR, keep=AUDIT_RETENTION_ZIPS
    )


def archive_old_application_logs() -> list[Path]:
    """Sweep loose application-log dailies into :data:`_APP_ZIP_DIR` and prune. See
    :func:`_sweep_loose_dailies`."""
    return _sweep_loose_dailies(
        base_name="logging.log", subdir=_APP_ZIP_DIR, keep=APP_LOG_RETENTION_ZIPS
    )


def setup_logging() -> None:
    """Configure INFO logging for the application (diagnostic) stream.

    Installs a daily-rotating file handler (``logging.log``, dailies compressed
    into :data:`_APP_ZIP_DIR`) and a stderr handler so the same records reach
    both the log file and the systemd journal. Runs the startup sweep once. Safe
    to call multiple times — only installs handlers once.
    """
    logger = logging.getLogger(_ROOT)
    if logger.handlers:
        return

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    file_handler = TimedRotatingFileHandler(
        log_dir / "logging.log", when="midnight", backupCount=0, encoding="utf-8", utc=True
    )
    _install_daily_zip_rotation(file_handler, subdir=_APP_ZIP_DIR, keep=APP_LOG_RETENTION_ZIPS)
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()  # stderr -> journal under gunicorn
    stream_handler.setFormatter(fmt)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False

    archive_old_application_logs()


def setup_audit_logging() -> None:
    """Configure the transaction/audit stream (``transactions.log``).

    A separate, daily-rotating file on its own ``audit`` logger; dailies are
    compressed into :data:`_TX_ZIP_DIR` as they roll and that folder is capped at
    :data:`AUDIT_RETENTION_ZIPS` archives. ``backupCount`` is 0 so the handler's
    own pruning never runs — retention is the rotator's job. The startup sweep
    runs once here. Safe to call multiple times.
    """
    logger = logging.getLogger(AUDIT_LOGGER)
    if logger.handlers:
        return

    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = TimedRotatingFileHandler(
        log_dir / "transactions.log", when="midnight", backupCount=0, encoding="utf-8", utc=True
    )
    _install_daily_zip_rotation(handler, subdir=_TX_ZIP_DIR, keep=AUDIT_RETENTION_ZIPS)
    handler.setFormatter(logging.Formatter(_AUDIT_FMT, datefmt=_DATE_FMT))

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False

    archive_old_transaction_logs()


def archive_log() -> None:
    """
    Move the current logging.log to app/logging/logs/run_<timestamp>.log.
    Called before a DB wipe so the old run is preserved.
    Closes the active FileHandler first so Windows releases the file lock.
    """
    log_file = _log_dir() / "logging.log"
    if not log_file.exists():
        return

    # Close and detach every FileHandler pointing at logging.log. The audit
    # logger is a separate logger and is left untouched.
    root = logging.getLogger(_ROOT)
    for handler in root.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            root.removeHandler(handler)

    archive_dir = _log_dir() / "logs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    log_file.rename(archive_dir / f"run_{ts}.log")
