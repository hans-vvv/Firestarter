"""Symmetric set-diff between rendered and live normalised configs, producing DiffResult per device."""

from __future__ import annotations

from dataclasses import dataclass

from app.compliance.normaliser import NormalisedConfig

# ------------------------------------------------------------------
# Result containers
# ------------------------------------------------------------------


@dataclass(frozen=True)
class DiffResult:
    """Symmetric diff outcome for a single device.

    Attributes
    ----------
    hostname:
        Device the diff belongs to.
    only_in_rendered:
        Lines present in the rendered (intended) config but absent from
        the live device.  These represent configuration that should be
        on the device but is not — i.e. missing config.
    only_in_live:
        Lines present on the live device but absent from the rendered
        config.  These represent configuration that is on the device but
        was not intended — i.e. unexpected or stale config.
    ignored_rendered:
        Number of lines suppressed by ignore rules on the rendered side.
    ignored_live:
        Number of lines suppressed by ignore rules on the live side.
    """

    hostname: str
    only_in_rendered: frozenset[str]
    only_in_live: frozenset[str]
    ignored_rendered: int
    ignored_live: int

    @property
    def compliant(self) -> bool:
        """True when there is no drift in either direction."""
        return not self.only_in_rendered and not self.only_in_live

    @property
    def drift_count(self) -> int:
        """Total number of differing lines across both sides."""
        return len(self.only_in_rendered) + len(self.only_in_live)


# ------------------------------------------------------------------
# Differ
# ------------------------------------------------------------------


class ComplianceDiffer:
    """Produce a symmetric set diff between rendered and live configs.

    Why symmetric diff
    ------------------
    A one-sided diff (rendered minus live) only catches missing config.
    A symmetric diff catches both directions:

    - ``only_in_rendered`` — intended config absent from the device
      (something the renderer wants but the device does not have).
    - ``only_in_live``     — unintended config present on the device
      (something on the device that the renderer does not know about).

    Both directions matter for compliance.  A device could be fully
    missing a service, or it could have a stale service from a previous
    deployment that was never cleaned up.

    The diff is a pure set operation — O(n) and no ordering dependency —
    because the Normaliser already produced frozensets.
    """

    def diff(
        self,
        *,
        rendered: NormalisedConfig,
        live: NormalisedConfig,
    ) -> DiffResult:
        """Diff *rendered* against *live* for a single device.

        Parameters
        ----------
        rendered:
            Normalised config from the Jinja2 renderer (disk file).
        live:
            Normalised config fetched from the device via Netmiko.

        Returns
        -------
        DiffResult
            Immutable result carrying both diff directions and ignore
            counts from both sides.

        Raises
        ------
        ValueError
            When *rendered* and *live* belong to different hostnames.
            This is a programming error in the Runner — the Differ never
            silently compares configs from two different devices.
        """
        if rendered.hostname != live.hostname:
            raise ValueError(
                f"Hostname mismatch: rendered is for {rendered.hostname!r} "
                f"but live is for {live.hostname!r}. "
                "The Runner must pair configs from the same device."
            )

        return DiffResult(
            hostname=rendered.hostname,
            only_in_rendered=rendered.lines - live.lines,
            only_in_live=live.lines - rendered.lines,
            ignored_rendered=rendered.ignored_count,
            ignored_live=live.ignored_count,
        )

    def diff_many(
        self,
        *,
        rendered: dict[str, NormalisedConfig],
        live: dict[str, NormalisedConfig],
    ) -> dict[str, DiffResult]:
        """Diff rendered vs live configs for a fleet of devices.

        Only diffs hostnames present in BOTH dicts.  Hostnames present
        in one side but not the other are skipped — the Runner is
        responsible for logging those as fetch or render failures before
        calling this method.

        Parameters
        ----------
        rendered:
            Mapping of ``hostname → NormalisedConfig`` from disk.
        live:
            Mapping of ``hostname → NormalisedConfig`` from Netmiko.

        Returns
        -------
        dict[str, DiffResult]
            Mapping of ``hostname → DiffResult`` for every hostname
            present on both sides.
        """
        common = rendered.keys() & live.keys()

        return {
            hostname: self.diff(
                rendered=rendered[hostname],
                live=live[hostname],
            )
            for hostname in sorted(common)
        }
