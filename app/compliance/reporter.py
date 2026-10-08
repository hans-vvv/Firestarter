"""Formats and writes a human-readable compliance report to disk."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.compliance.differ import DiffResult
from app.compliance.runner import ComplianceRunSummary

# Column width for device hostname in the summary table
_HOSTNAME_COL = 40


class ComplianceReporter:
    """Format and write a human-readable compliance report.

    The report has three sections:

    1. **Run summary** — one line per device showing compliant / drift
       count / ignored counts.  Failures are listed separately.
    2. **Drift detail** — for every non-compliant device, two labelled
       blocks showing missing lines (only in rendered) and unexpected
       lines (only in live).  Sorted alphabetically within each block
       so the output is stable across runs.
    3. **Footer** — timestamp and totals.

    Output targets
    --------------
    - Optionally written to a file under ``report_dir`` when
      ``report_dir`` is supplied.  The filename is
      ``compliance_<timestamp>.txt`` so reports from successive runs
      never overwrite each other.
    - The full report text is always returned from ``report()`` so the
      caller can print or process it as needed.

    Parameters
    ----------
    report_dir:
        Optional directory to write the report file into.  Created
        (including parents) if it does not already exist.
    """

    def __init__(self, *, report_dir: Path | None = None) -> None:
        self._report_dir = report_dir
        if report_dir is not None:
            report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def report(self, *, summary: ComplianceRunSummary) -> str:
        """Render and optionally write the compliance report.

        Parameters
        ----------
        summary:
            Run summary produced by ``ComplianceRunner.run()``.

        Returns
        -------
        str
            The full report text.  Print it, log it, or discard it —
            the reporter does not make that decision for the caller.
        """
        now = datetime.now(UTC)
        text = self._render(summary=summary, timestamp=now)

        if self._report_dir is not None:
            path = self._report_path(timestamp=now)
            path.write_text(text, encoding="utf-8")

        return text

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(
        self,
        *,
        summary: ComplianceRunSummary,
        timestamp: datetime,
    ) -> str:
        """Assemble the full report text from its constituent sections.

        Sections are built independently and joined with newlines.
        The drift detail section is omitted when all devices are
        compliant.  The failures section is omitted when there are none.
        """
        sections: list[str] = []

        sections.append(self._render_header(timestamp=timestamp))
        sections.append(self._render_summary_table(summary=summary))

        non_compliant = {h: r for h, r in summary.results.items() if not r.compliant}
        if non_compliant:
            sections.append(self._render_drift_detail(results=non_compliant))

        if summary.failed_render or summary.failed_fetch:
            sections.append(self._render_failures(summary=summary))

        sections.append(self._render_footer(summary=summary))

        return "\n".join(sections)

    def _render_header(self, *, timestamp: datetime) -> str:
        """Render the report header with a UTC timestamp."""
        ts = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        bar = "=" * 72
        return f"{bar}\n  COMPLIANCE REPORT — {ts}\n{bar}"

    def _render_summary_table(self, *, summary: ComplianceRunSummary) -> str:
        """Render the per-device summary table.

        One line per device showing status, total drift line count, and
        the number of lines ignored on the rendered (R) and live (L)
        sides.  The ignored counts are shown as ``R/L`` — a large
        asymmetry here is a signal that ignore rules are only firing on
        one side, which usually indicates a normalisation bug rather
        than real drift.

        Devices that failed to render or fetch are appended below the
        main table with a descriptive status label.
        """
        lines: list[str] = []
        lines.append("\nSUMMARY\n" + "-" * 72)

        header = f"{'DEVICE':<{_HOSTNAME_COL}}{'STATUS':<14}{'DRIFT':>6}{'IGNORED (R/L)':>16}"
        lines.append(header)
        lines.append("-" * 72)

        for hostname in sorted(summary.results):
            result = summary.results[hostname]
            status = "COMPLIANT" if result.compliant else "DRIFT"
            drift = str(result.drift_count) if not result.compliant else "-"
            ignored = f"{result.ignored_rendered}/{result.ignored_live}"
            lines.append(f"{hostname:<{_HOSTNAME_COL}}{status:<14}{drift:>6}{ignored:>16}")

        for hostname in sorted(summary.failed_render):
            lines.append(f"{hostname:<{_HOSTNAME_COL}}{'RENDER FAIL':<14}")

        for hostname in sorted(summary.failed_fetch):
            lines.append(f"{hostname:<{_HOSTNAME_COL}}{'FETCH FAIL':<14}")

        compliant = summary.compliant_count
        total = len(summary.results)
        lines.append("-" * 72)
        lines.append(f"  {compliant}/{total} devices compliant")

        return "\n".join(lines)

    def _render_drift_detail(
        self,
        *,
        results: dict[str, DiffResult],
    ) -> str:
        """Render the full drift detail for every non-compliant device.

        For each device two blocks are shown when applicable:

        - **MISSING** — lines present in the rendered (intended) config
          but absent from the live device.  These represent config that
          should be pushed to the device.
        - **UNEXPECTED** — lines present on the live device but absent
          from the rendered config.  These represent stale or unintended
          config that should be investigated.

        Lines within each block are sorted alphabetically so the output
        is deterministic and can be compared across runs with standard
        text diff tools.  The ``-`` / ``+`` prefixes follow the
        conventional unified diff notation.
        """
        lines: list[str] = []
        lines.append("\nDRIFT DETAIL\n" + "-" * 72)

        for hostname in sorted(results):
            result = results[hostname]
            lines.append(f"\n  {hostname}")
            lines.append(f"  {'─' * (len(hostname))}")

            if result.only_in_rendered:
                lines.append(
                    f"\n  MISSING from device "
                    f"({len(result.only_in_rendered)} lines)"
                    f" — in rendered but not on device:"
                )
                for line in sorted(result.only_in_rendered):
                    lines.append(f"    - {line}")

            if result.only_in_live:
                lines.append(
                    f"\n  UNEXPECTED on device "
                    f"({len(result.only_in_live)} lines)"
                    f" — on device but not in rendered:"
                )
                for line in sorted(result.only_in_live):
                    lines.append(f"    + {line}")

        return "\n".join(lines)

    def _render_failures(self, *, summary: ComplianceRunSummary) -> str:
        """Render the failures section listing devices that could not be diffed.

        A render failure means the Jinja2 renderer raised an exception
        for that device — typically a missing DB record or template error.
        A fetch failure means ``backup.py`` could not reach the device or
        the fetch result carried an error.  Both types of failure result
        in the device being excluded from the diff entirely.
        """
        lines: list[str] = []
        lines.append("\nFAILURES\n" + "-" * 72)

        if summary.failed_render:
            lines.append("  Render failures (config could not be generated):")
            for hostname in sorted(summary.failed_render):
                lines.append(f"    {hostname}")

        if summary.failed_fetch:
            lines.append("  Fetch failures (device unreachable or auth failed):")
            for hostname in sorted(summary.failed_fetch):
                lines.append(f"    {hostname}")

        return "\n".join(lines)

    def _render_footer(self, *, summary: ComplianceRunSummary) -> str:
        """Render the footer with a one-line summary of run totals."""
        bar = "=" * 72
        return (
            f"\n{bar}\n"
            f"  attempted={summary.total_attempted}  "
            f"compliant={summary.compliant_count}  "
            f"drift={summary.non_compliant_count}  "
            f"render_fail={len(summary.failed_render)}  "
            f"fetch_fail={len(summary.failed_fetch)}\n"
            f"{bar}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _report_path(self, *, timestamp: datetime) -> Path:
        """Build the output file path from the run timestamp.

        Colons are replaced with dashes so the filename is valid on
        Windows (NTFS does not allow colons in filenames).
        """
        ts = timestamp.strftime("%Y-%m-%dT%H-%M-%S")
        return self._report_dir / f"compliance_{ts}.txt"  # type: ignore[operator]
