"""Build the production-data ZIP bundle for download.

A bundle is a plain ZIP holding every file :mod:`app.data_bundle.spec` classifies
as production data, plus a ``manifest.json`` describing the snapshot (source host,
environment label, database schema revision, and a SHA-256 + size per file).

The bundle is **download-only**: it exists so an operator can pull an environment's
state out (for safekeeping, or to seed a development worktree). It is never
uploaded back through the web app — restoring is a filesystem step, done where you
have shell access: unzip the bundle at the repo root and every file lands at the
relative path recorded in the manifest. Keeping it one-directional removes the
whole class of risk around overwriting a live database through a browser.

The module is Flask-free and takes ``data_root`` explicitly so tests can drive it
against a temporary tree. File ordering and ZIP mtimes are pinned, so the static
members (YAML, workbook) are byte-stable across rebuilds. The ``app.db`` member is
a consistent SQLite snapshot taken via the backup API (WAL-safe), which is not
guaranteed byte-identical run-to-run — but a live database file was never stable
under a plain copy either, so this only makes an existing reality explicit.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import sqlite3
import zipfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.data_bundle.spec import resolve_bundle_files
from app.db.snapshot import snapshot_sqlite_bytes

MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

# Fixed ZIP member timestamp (1980-01-01, the ZIP epoch) so bundle bytes depend
# only on file contents, not on when the files happened to be written.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ManifestEntry:
    """One file's integrity record inside the manifest."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Manifest:
    """The bundle's self-description, serialised as ``manifest.json``.

    Everything here is informational for whoever later unpacks the bundle: which
    server and environment it came from, what database schema revision the data is
    at (so they know whether an ``alembic upgrade`` is due), and a checksum per
    file so a truncated or corrupted download can be spotted.
    """

    format_version: int
    created_at: str
    source_host: str
    environment: str
    alembic_revision: str | None
    files: list[ManifestEntry]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_alembic_revision(db_path: Path) -> str | None:
    """Best-effort read of the SQLite DB's current Alembic revision.

    Returns ``None`` if the file is absent, is not a SQLite DB, or has no
    ``alembic_version`` table (a DB that predates migrations). Never raises — the
    revision is advisory metadata, not something the build hinges on.
    """
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        finally:
            conn.close()
        return None if row is None else str(row[0])
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_bundle(*, data_root: Path, environment: str, source_host: str | None = None) -> bytes:
    """Build a production-data bundle from *data_root* and return the ZIP bytes.

    ``environment`` is the label recorded in the manifest (e.g. ``"production"``,
    ``"test"``) so whoever unpacks the bundle can see where it came from.
    ``source_host`` defaults to this machine's hostname.
    """
    files = resolve_bundle_files(data_root=data_root)

    # Read each file's bytes exactly once, so the manifest checksum and the ZIP
    # member are guaranteed to describe the same content. SQLite databases are
    # captured via the WAL-safe backup API (a plain read of a live ``.db`` can
    # miss just-committed rows still sitting in the ``-wal`` sidecar).
    contents: list[tuple[Path, bytes]] = []
    for rel in files:
        src = data_root / rel
        data = snapshot_sqlite_bytes(src) if rel.suffix == ".db" else src.read_bytes()
        contents.append((rel, data))

    entries = [
        ManifestEntry(
            path=rel.as_posix(),
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        for rel, data in contents
    ]

    manifest = Manifest(
        format_version=FORMAT_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        source_host=source_host or socket.gethostname(),
        environment=environment,
        alembic_revision=_db_alembic_revision(data_root / "app.db"),
        files=entries,
    )

    # Stamp deterministic, group-readable modes into the archive so a landed bundle
    # needs no `chmod -R g+rX` fix-up: files rw-r----- (640), directories rwxr-x---
    # (750). The `developers` group can read and traverse; `other` gets nothing, so
    # app.db (which carries dashboard account hashes) stays unreadable outside the
    # group. Without this, a ZIP built under a normal umask lands mode 600 and locks
    # the group out of the whole data set — breaking seed_worktree for everyone.
    file_mode = 0o100640 << 16
    dir_mode = 0o040750 << 16

    dir_entries = sorted(
        {
            parent.as_posix() + "/"
            for rel, _ in contents
            for parent in rel.parents
            if parent.as_posix() != "."
        }
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for d in dir_entries:
            di = zipfile.ZipInfo(d, date_time=_ZIP_EPOCH)
            di.external_attr = dir_mode
            zf.writestr(di, b"")
        for rel, data in contents:
            info = zipfile.ZipInfo(rel.as_posix(), date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = file_mode
            zf.writestr(info, data)
        manifest_info = zipfile.ZipInfo(MANIFEST_NAME, date_time=_ZIP_EPOCH)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = file_mode
        zf.writestr(manifest_info, manifest.to_json())
    return buf.getvalue()
