"""Orchestrates the compliance pipeline: render → fetch → normalise → diff."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.automation.backup import BACKUPS_DIR, load_results
from app.compliance.differ import ComplianceDiffer, DiffResult
from app.compliance.normaliser import ComplianceNormaliser, NormalisedConfig
from app.printing.printer import Printer

# ------------------------------------------------------------------
# Run summary container
# ------------------------------------------------------------------


@dataclass
class ComplianceRunSummary:
    """Aggregate outcome of a full compliance run.

    Attributes
    ----------
    results:
        Mapping of ``hostname → DiffResult`` for every device that was
        successfully rendered, fetched, and diffed.
    failed_render:
        Hostnames whose rendered-side normalisation raised an exception
        (typically a malformed ignore YAML or extra .cfg file).
    failed_live:
        Hostnames whose live-side (fetched) normalisation raised an
        exception.  Same likely cause as ``failed_render`` — surfacing
        the failure prevents the device from silently disappearing from
        the report.
    failed_fetch:
        Hostnames whose fetch result carried an error from backup.py.
    skipped:
        Hostnames present on only one side after failures.
    """

    results: dict[str, DiffResult] = field(default_factory=dict)
    failed_render: list[str] = field(default_factory=list)
    failed_live: list[str] = field(default_factory=list)
    failed_fetch: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def compliant_count(self) -> int:
        """Number of devices with zero configuration drift."""
        return sum(1 for r in self.results.values() if r.compliant)

    @property
    def non_compliant_count(self) -> int:
        """Number of devices with at least one diff line."""
        return sum(1 for r in self.results.values() if not r.compliant)

    @property
    def total_attempted(self) -> int:
        """Total devices attempted: diffed + render/live failures + fetch failures."""
        return (
            len(self.results)
            + len(self.failed_render)
            + len(self.failed_live)
            + len(self.failed_fetch)
        )


# ------------------------------------------------------------------
# Runner
# ------------------------------------------------------------------


class ComplianceRunner:
    """Orchestrate the full compliance pipeline for a fleet of devices.

    Pipeline order
    --------------
    1. Printer    — render all device configs and write to disk under
                    ``app/artifacts/latest/``.  Also returns the rendered
                    text in memory so we avoid reading files back from disk.
    2. Fetcher    — load the most recent local backup run from
                    ``app/backups/latest/``: ``<hostname>.cfg`` for the
                    configs, ``fetch_results.json`` for per-device outcomes.
                    Refreshing it is a separate step
                    (``app/automation/backup.py``).
    3. Normaliser — strip noise, apply ignore rules, inject extra lines.
                    ``normalise_rendered()`` is used for the rendered side
                    so that ``app/compliance/extra/<hostname>.cfg`` files
                    are included.  ``normalise()`` is used for the live side.
    4. Differ     — symmetric set diff per device.

    Failures are isolated per device.  A render failure on device A does
    not prevent device B from being diffed.

    Parameters
    ----------
    session:
        SQLAlchemy session passed to the Printer and Normaliser.
    hostnames:
        Optional allow-list of hostnames to target.  When omitted all
        devices with computed service instances are included on the
        rendered side, and every device in the last backup run on the live
        side.
    backups_dir:
        Root of the config-backup store written by
        ``app/automation/backup.py``.  The live side is read from its
        ``latest/`` directory.
    """

    def __init__(
        self,
        *,
        session: Session,
        hostnames: list[str] | None = None,
        backups_dir: Path = BACKUPS_DIR,
    ) -> None:
        self._session = session
        self._hostnames = hostnames
        self._backups_dir = backups_dir
        self._printer = Printer(session=session)
        self._normaliser = ComplianceNormaliser(session=session)
        self._differ = ComplianceDiffer()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> ComplianceRunSummary:
        """Execute the full compliance pipeline and return a run summary."""
        summary = ComplianceRunSummary()

        # ----------------------------------------------------------
        # Stage 1: render and write configs to disk
        # ----------------------------------------------------------
        rendered_raw = self._render_all(summary=summary)

        if not rendered_raw:
            raise RuntimeError("Compliance run aborted: no configs were rendered.")

        # ----------------------------------------------------------
        # Stage 2: load live configs from pickle
        # ----------------------------------------------------------
        live_raw = self._load_live(summary=summary)

        # ----------------------------------------------------------
        # Stage 3: apply hostname filter if requested
        # ----------------------------------------------------------
        if self._hostnames:
            rendered_raw = {h: v for h, v in rendered_raw.items() if h in self._hostnames}
            live_raw = {h: v for h, v in live_raw.items() if h in self._hostnames}

        # ----------------------------------------------------------
        # Stage 4: normalise both sides
        # ----------------------------------------------------------
        rendered_norm = self._normalise_rendered(raw_configs=rendered_raw, summary=summary)
        live_norm = self._normalise_live(raw_configs=live_raw, summary=summary)

        # Collect hostnames present on only one side.
        rendered_only = rendered_norm.keys() - live_norm.keys()
        live_only = live_norm.keys() - rendered_norm.keys()
        for hostname in sorted(rendered_only | live_only):
            summary.skipped.append(hostname)

        # ----------------------------------------------------------
        # Stage 5: diff
        # ----------------------------------------------------------
        summary.results = self._differ.diff_many(
            rendered=rendered_norm,
            live=live_norm,
        )

        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _render_all(self, *, summary: ComplianceRunSummary) -> dict[str, str]:
        """Render all devices, write to disk, return rendered text by hostname."""
        try:
            return self._printer.write_to_disk()
        except Exception as exc:
            raise RuntimeError(f"Render stage failed: {exc}") from exc

    def _load_live(self, *, summary: ComplianceRunSummary) -> dict[str, str]:
        """Build the live side from the most recent config-backup run.

        One source now, written locally by ``app/automation/backup.py``: the
        ``.cfg`` files carry the configs and ``fetch_results.json`` carries the
        per-device outcome.  A device that could not be reached is recorded in
        ``summary.failed_fetch`` rather than silently missing, which is what
        keeps "drifted" distinguishable from "unreachable" in the report.

        An empty mapping is a legitimate state — a checkout where no backup has
        been run yet simply has nothing to compare against.
        """
        live_raw: dict[str, str] = {}
        for hostname, result in load_results(backups_dir=self._backups_dir).items():
            if result.ok and result.raw is not None:
                live_raw[hostname] = result.raw
            else:
                summary.failed_fetch.append(hostname)
        return live_raw

    def _normalise_rendered(
        self,
        *,
        raw_configs: dict[str, str],
        summary: ComplianceRunSummary,
    ) -> dict[str, NormalisedConfig]:
        """Normalise the rendered side, injecting extra lines where present."""
        normalised: dict[str, NormalisedConfig] = {}

        for hostname, raw in raw_configs.items():
            try:
                normalised[hostname] = self._normaliser.normalise_rendered(
                    hostname=hostname,
                    raw=raw,
                )
            except Exception:
                summary.failed_render.append(hostname)

        return normalised

    def _normalise_live(
        self,
        *,
        raw_configs: dict[str, str],
        summary: ComplianceRunSummary,
    ) -> dict[str, NormalisedConfig]:
        """Normalise the live side. Per-device failures are collected, not raised.

        A hostname whose normalisation raises is recorded in
        ``summary.failed_live`` and excluded from the diff stage.
        Previously the exception was swallowed silently, which caused the
        device to disappear from the report with no indication of error.
        """
        normalised: dict[str, NormalisedConfig] = {}

        for hostname, raw in raw_configs.items():
            try:
                normalised[hostname] = self._normaliser.normalise(
                    hostname=hostname,
                    raw=raw,
                )
            except Exception:
                summary.failed_live.append(hostname)

        return normalised
