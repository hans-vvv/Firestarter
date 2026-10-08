"""Named filesystem path constants used across the service layer.

Every per-environment **data** file and every **writable output** directory the
app touches is declared here, as a path relative to a single ``DATA_ROOT``.

``DATA_ROOT`` defaults to ``<repo>/data`` (a single gitignored directory holding
no code), so a plain checkout and its worktrees work with zero configuration.  It
is overridden by the ``FIRESTARTER_DATA`` environment variable — the one seam
that lets a deployment point at an external, project-namespaced root
(``/srv/firestarter-data``) or a container mount all mutable state on a single
volume (``/data``), while the repository / image stays pure code.  This is the
runtime counterpart to the download-bundle taxonomy in ``app/data_bundle/spec.py``
(which describes the same files for the seed/bundle path).  See ``docs/adr/0001``
(code vs environment data).

Code — Python, Jinja2 templates under ``app/services/templates`` — is NOT
declared here: it travels in git / the image and is resolved relative to its own
module, never relative to ``DATA_ROOT``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _repo_root() -> Path:
    """The repository root — two levels above this module (``app/domain/``)."""
    return Path(__file__).resolve().parents[2]


def data_root() -> Path:
    """Base directory for all environment data and writable output.

    Read **live** from ``FIRESTARTER_DATA`` on every call (not frozen at import)
    so a container, or a test, can repoint it without re-importing.  Falls back
    to ``<repo>/data`` for a plain checkout — a single gitignored directory that
    holds no code, keeping per-worktree isolation with zero configuration.
    Real deployments set ``FIRESTARTER_DATA`` to an external, project-namespaced
    root (e.g. ``/srv/firestarter-data``, or ``/data`` in a container) so the
    repository / image stays pure code.
    """
    env = os.getenv("FIRESTARTER_DATA")
    return Path(env) if env else _repo_root() / "data"


@dataclass(frozen=True)
class FileLocations:
    """A filesystem path, declared relative to :func:`data_root`.

    ``location`` is the data-root-relative string — still the currency of the
    download-bundle / seed taxonomy and a couple of tests.  ``path`` resolves it
    against the live data root, giving a CWD-independent absolute path.
    """

    location: str

    @property
    def path(self) -> Path:
        """Absolute path under the live :func:`data_root`."""
        return data_root() / self.location


# ---------------------------------------------------------------------------
# Inputs (read)
# ---------------------------------------------------------------------------

ADDRESSING_DEF_LOC = FileLocations(
    location="services/addressing",
)


SERVICES_DEF_LOC = FileLocations(
    location="services/definitions",
)


TOPOLOGY_EXCEL_LOC = FileLocations(
    location="topology.xlsx",
)

PRODUCTION_DB_LOC = FileLocations(
    location="app.db",
)

# Compliance reference inputs (per-environment data). The call sites glob out
# only the data (``*.yaml`` / ``*.cfg``), exactly as the bundle taxonomy does.
COMPLIANCE_IGNORE_LOC = FileLocations(
    location="compliance/ignore",
)

COMPLIANCE_EXTRA_LOC = FileLocations(
    location="compliance/extra",
)

COMPLIANCE_REMEDIATION_LOC = FileLocations(
    location="compliance/remediation",
)

# Simulated-device inputs (``drift.yaml``): the deviations the demo's simulated
# devices show versus intent. Environment data like the compliance inputs — the
# demo ships a drift file, a real deployment has no use for one.
SIMULATION_LOC = FileLocations(
    location="simulation",
)


# ---------------------------------------------------------------------------
# Outputs (written at runtime — live on the same writable data root)
# ---------------------------------------------------------------------------

COMPLIANCE_REPORTS_LOC = FileLocations(
    location="compliance/reports",
)

BACKUPS_LOC = FileLocations(
    location="backups",
)

ARTIFACTS_LOC = FileLocations(
    location="artifacts",
)

# Generated Nornir inventory (hosts.yaml / groups.yaml) — derived from the DB and
# rewritten on every run. An output, so it belongs under the data root rather than
# the code tree (letting the image run read-only).
GENERATED_LOC = FileLocations(
    location="automation/generated",
)

# Application logs. Overridable by FIRESTARTER_LOG_DIR (see app/logging/logger.py);
# this is the default when that is unset.
LOGS_LOC = FileLocations(
    location="logs",
)
