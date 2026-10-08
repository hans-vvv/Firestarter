"""Fetch live device configurations with Nornir and store them for compliance.

This replaces the previous arrangement, where a standalone script ran on a
separate VM, pickled its results, and the compliance module pulled that pickle
over SCP. Nornir now runs on the same host as the application, so the configs are
simply written to disk where compliance reads them — no transfer, no pickle, and
no credentials in the repository.

Layout under ``app/backups/``::

    latest/
        <hostname>.cfg        one flat config per device that responded
        datetime.txt          UTC timestamp of the run
        fetch_results.json    per-device outcome, including failures
    2026-07-21T09-44-53/      previous run, archived on the next one

``latest/`` is archived rather than overwritten, so a bad fetch never destroys
the last good set. The archive is named for the timestamp *the data was
collected*, read from the directory's own ``datetime.txt``, not the time the
archiving run happened.

Devices that fail are deliberately **not** written as ``.cfg`` files — a
truncated or missing config must never look like a real one to the compliance
differ. Their error is recorded in ``fetch_results.json`` instead, which is what
lets the compliance report distinguish "this device has drifted" from "we could
not reach this device".

Credentials are never stored. The CLI prompts at run time.
"""

from __future__ import annotations

import getpass
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.automation.commands import CommandResult, run_commands
from app.domain.file_locations import BACKUPS_LOC

# Resolved under the live data root (repo root by default, FIRESTARTER_DATA when
# set) so this is correct regardless of the process CWD.
BACKUPS_DIR = BACKUPS_LOC.path
LATEST_DIRNAME = "latest"

# How many *archived* runs to keep on the server. Each backup run archives the
# previous ``latest/`` under its collection timestamp, so these accumulate without
# bound unless pruned. The current ``latest/`` is always kept on top of this count.
# This is the quick-change knob — bump it here and nothing else needs touching.
MAX_BACKUPS = 20

CONFIG_SUFFIX = ".cfg"
TIMESTAMP_FILENAME = "datetime.txt"
RESULTS_FILENAME = "fetch_results.json"

# Flat output is what the compliance normaliser expects; "| no-more" suppresses
# paging so the whole config arrives in one read.
FETCH_COMMAND = "admin show configuration flat | no-more"

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchResult:
    """Outcome of fetching one device's configuration.

    Defined here, next to the code that produces it, and imported by the
    compliance module — previously the same shape was declared in two places and
    reconciled at unpickling time.
    """

    hostname: str
    raw: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def latest_dir(*, backups_dir: Path = BACKUPS_DIR) -> Path:
    """Directory holding the most recent run."""
    return backups_dir / LATEST_DIRNAME


def archive_latest(*, backups_dir: Path = BACKUPS_DIR) -> Path | None:
    """Move an existing ``latest/`` aside, returning where it went.

    Named for the timestamp recorded *inside* the directory, so the label
    reflects when the data was collected. Falls back to the directory mtime when
    ``datetime.txt`` is missing, and disambiguates with a numeric suffix if two
    runs land in the same second.
    """
    latest = latest_dir(backups_dir=backups_dir)
    if not latest.exists():
        return None

    stamp_file = latest / TIMESTAMP_FILENAME
    if stamp_file.exists():
        timestamp = stamp_file.read_text(encoding="utf-8").strip()
    else:
        mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)
        timestamp = mtime.strftime(TIMESTAMP_FORMAT)

    # Colons are legal here but make the directories awkward to handle, and the
    # archives were historically created on Windows.
    safe = timestamp.replace(":", "-")
    target = backups_dir / safe
    suffix = 1
    while target.exists():
        target = backups_dir / f"{safe}_{suffix}"
        suffix += 1

    log.info("archiving %s -> %s", latest, target)
    shutil.move(str(latest), str(target))
    return target


def prune_old_backups(*, backups_dir: Path = BACKUPS_DIR, keep: int = MAX_BACKUPS) -> list[Path]:
    """Delete archived runs beyond the newest *keep*, returning what was removed.

    An archived run is any directory under ``backups_dir`` other than ``latest/``.
    They are named for their collection timestamp
    (``YYYY-MM-DDTHH-MM-SS`` with a ``_N`` suffix on collisions), which sorts
    lexicographically in chronological order — so the newest *keep* are simply the
    tail of the sorted list, and everything before it is old enough to drop.

    ``latest/`` is never a candidate: it is the current working set, not an archive.
    Stray files (never created here) are ignored. A no-op when at or under the limit.
    """
    if not backups_dir.exists():
        return []

    archives = sorted(p for p in backups_dir.iterdir() if p.is_dir() and p.name != LATEST_DIRNAME)
    if len(archives) <= keep:
        return []

    to_remove = archives[: len(archives) - keep]
    for path in to_remove:
        log.info("pruning old backup %s", path)
        shutil.rmtree(path)
    return to_remove


def write_run(
    *,
    results: dict[str, FetchResult],
    timestamp: str,
    backups_dir: Path = BACKUPS_DIR,
) -> Path:
    """Write one run's configs, timestamp and outcomes into ``latest/``.

    Assumes ``latest/`` has already been archived; creates it fresh.
    """
    latest = latest_dir(backups_dir=backups_dir)
    latest.mkdir(parents=True, exist_ok=True)

    for hostname, result in sorted(results.items()):
        if not result.ok or result.raw is None:
            continue
        # A hostname reaches us from the inventory, which is generated from the
        # database, but it still ends up as a filename — keep it a bare name.
        safe_name = hostname.replace("/", "_").replace(" ", "_")
        text = result.raw if result.raw.endswith("\n") else result.raw + "\n"
        (latest / f"{safe_name}{CONFIG_SUFFIX}").write_text(text, encoding="utf-8")

    (latest / TIMESTAMP_FILENAME).write_text(timestamp + "\n", encoding="utf-8")

    # Only the outcome goes in the JSON; the configs themselves are the .cfg
    # files, so nothing is stored twice.
    payload = {
        "timestamp": timestamp,
        "results": {
            hostname: {"ok": result.ok, "error": result.error}
            for hostname, result in sorted(results.items())
        },
    }
    (latest / RESULTS_FILENAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return latest


def load_results(*, backups_dir: Path = BACKUPS_DIR) -> dict[str, FetchResult]:
    """Read back a run's outcomes, pairing each with its ``.cfg`` contents.

    Returns an empty mapping when no run has been recorded yet, so a fresh
    checkout degrades to "no live configs" rather than raising.
    """
    latest = latest_dir(backups_dir=backups_dir)
    results_file = latest / RESULTS_FILENAME
    if not results_file.exists():
        return {}

    payload = json.loads(results_file.read_text(encoding="utf-8"))
    out: dict[str, FetchResult] = {}
    for hostname, entry in payload.get("results", {}).items():
        cfg = latest / f"{hostname}{CONFIG_SUFFIX}"
        raw = cfg.read_text(encoding="utf-8") if cfg.exists() else None
        out[hostname] = FetchResult(hostname=hostname, raw=raw, error=entry.get("error"))
    return out


def read_backup(hostname: str, *, backups_dir: Path = BACKUPS_DIR) -> FetchResult | None:
    """Return the latest stored backup outcome for one host, or ``None`` if unknown.

    ``None`` means the most recent run has no record of *hostname* — either no run
    has happened yet, or the device was not in that run's inventory. That is
    distinct from a recorded *failure* (a ``FetchResult`` whose ``error`` is set
    and ``raw`` is ``None``), so the caller can tell "we never tried" apart from
    "we tried and it failed".

    Only hosts present as keys in the run's own ``fetch_results.json`` are served,
    which is the path-safety guard: a hostname from a URL that is not a recorded
    key returns ``None`` before any file path is built from it. Reading one entry
    (rather than :func:`load_results`, which reads every ``.cfg``) keeps the
    per-device config panel cheap.
    """
    latest = latest_dir(backups_dir=backups_dir)
    results_file = latest / RESULTS_FILENAME
    if not results_file.exists():
        return None

    payload = json.loads(results_file.read_text(encoding="utf-8"))
    entry = payload.get("results", {}).get(hostname)
    if entry is None:
        return None

    cfg = latest / f"{hostname}{CONFIG_SUFFIX}"
    # Defensive: even though the hostname is a key from our own JSON, confirm the
    # resolved config path stays inside latest/ before reading it.
    raw = None
    if cfg.exists() and cfg.resolve().parent == latest.resolve():
        raw = cfg.read_text(encoding="utf-8")
    return FetchResult(hostname=hostname, raw=raw, error=entry.get("error"))


def read_timestamp(*, backups_dir: Path = BACKUPS_DIR) -> str | None:
    """Return the collection timestamp of the current ``latest/`` run, or ``None``.

    ``None`` when no run has been recorded yet (fresh checkout), so callers can
    render "no backups yet" rather than a blank timestamp.
    """
    stamp = latest_dir(backups_dir=backups_dir) / TIMESTAMP_FILENAME
    if not stamp.exists():
        return None
    return stamp.read_text(encoding="utf-8").strip() or None


def to_fetch_results(
    command_results: dict[str, list[CommandResult]],
) -> dict[str, FetchResult]:
    """Flatten single-command results into the per-device shape compliance reads.

    ``run_commands`` is general — a list of results per host, one per command.
    The config fetch always asks for exactly one command, so the first result is
    the whole story. Compliance keeps its own ``FetchResult`` shape rather than
    consuming ``CommandResult`` directly: it is what ``fetch_results.json``
    serialises, and changing it would change a file format for no gain.
    """
    return {
        hostname: FetchResult(
            hostname=hostname,
            raw=results[0].output,
            error=results[0].error,
        )
        for hostname, results in command_results.items()
    }


def run_backup(
    *,
    username: str,
    password: str,
    backups_dir: Path = BACKUPS_DIR,
    inventory_dir: Path | None = None,
) -> dict[str, FetchResult]:
    """Fetch every device in the generated inventory and record the run.

    The device I/O lives in :mod:`app.automation.commands`; this function owns
    only what is specific to configuration backups — which command to send, and
    how the run is archived and written to disk.
    """
    results = to_fetch_results(
        run_commands(
            username=username,
            password=password,
            commands=[FETCH_COMMAND],
            inventory_dir=inventory_dir,
        )
    )

    timestamp = datetime.now(UTC).strftime(TIMESTAMP_FORMAT)
    archive_latest(backups_dir=backups_dir)
    write_run(results=results, timestamp=timestamp, backups_dir=backups_dir)
    prune_old_backups(backups_dir=backups_dir)
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    username = input("SR OS username: ").strip()
    password = getpass.getpass(f"SR OS password for {username!r}: ")

    results = run_backup(username=username, password=password)

    failed = sorted(h for h, r in results.items() if not r.ok)
    log.info(
        "done — %d succeeded, %d failed — written to %s",
        len(results) - len(failed),
        len(failed),
        latest_dir(),
    )
    if failed:
        log.warning("failed devices: %s", ", ".join(failed))


if __name__ == "__main__":
    main()
