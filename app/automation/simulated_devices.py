"""Simulated SR OS devices — the demo's stand-in for a fleet of real routers.

The public demo has no devices to reach, yet the dashboard's *Latest backups* and
*Compliance* pages are only interesting when there is a live side to compare the
rendered intent against. This module manufactures that live side: a device's
"backup" is its own rendered configuration, reshaped into what a real SR OS MD-CLI
``admin show configuration flat`` dump looks like, with a set of deliberate
deviations ("drift") applied so compliance has something to find.

Why render-then-drift rather than hand-written backups: the rendered intent is the
only thing that can be relied on to match the templates exactly, so a drift-free
simulated backup normalises to the same line set as the intent and yields zero
findings — which makes every finding the compliance page shows attributable to a
line in the drift file, and nothing else.

Output shape
------------
A real dump prints a Nokia header (``# TiMOS-C-...``, ``# Generated <ts> by ...``),
one 4-space-indented statement per line (either ``configure <path> <value>`` or the
brace singleton ``configure { <path> <value> }``) and a ``# Finished <ts>`` trailer.
The compliance normaliser (``app/compliance/normaliser.py``) strips the ``#`` lines,
the indentation, the leading ``/`` of rendered lines and the braces, so the plain
indented form used here normalises identically to the rendered ``/configure ...``
lines. On top of the rendered lines every simulated device also emits the handful of
statements a real router always has but the renderer never produces (the local
``admin`` user, default log/QoS objects — see :data:`PLATFORM_LINES`); the demo's
``compliance/extra/base.cfg`` and ``compliance/ignore/base.yaml`` account for them,
exactly as they do on a real estate.

Drift file — ``data/simulation/drift.yaml``
-------------------------------------------
Deviations the simulated devices show versus the intended configuration, keyed by
hostname. All line values are written **without** the leading ``/`` (the normalised
form, the same way ``compliance/ignore/*.yaml`` values are written)::

    devices:
      pe1.Site10:
        remove:            # lines present in intent but "missing" on the device
          - startswith: 'configure router "Base" bgp neighbor "10.0.0.4"'
          - exact: 'configure port 1/1/c3/1 admin-state enable'
          - regex: 'isis 0 interface "lag-\\d+" hello-authentication-keychain'
        replace:           # intent line -> what the device has instead (exact match)
          - from: 'configure lag "lag-2" description "Remote: pe1.Site11:lag-2"'
            to:   'configure lag "lag-2" description "Remote: pe1.Site11:lag-1 (TEMP)"'
        add:               # lines on the device that intent never produced
          - 'configure router "Base" static-routes route 192.168.99.0/24 ...'
    unreachable:           # devices whose fetch fails (shown as failed backups)
      pe1.Site19: "TCP connection to device failed."

Every key is optional; a missing file means no drift at all. A ``remove`` or
``replace`` rule that matches nothing is logged as a warning rather than raised, so a
template change that renames a line does not break the demo — it just drops that
deviation until the drift file is updated.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.backup import (
    BACKUPS_DIR,
    TIMESTAMP_FORMAT,
    FetchResult,
    archive_latest,
    prune_old_backups,
    write_run,
)
from app.domain.file_locations import SIMULATION_LOC
from app.models import Device, DeviceStatus
from app.printing.printer import Printer
from app.services.service_handling.ce_lags import find_ce_lags

log = logging.getLogger(__name__)

# Module-level so tests can repoint it at a tmp file; resolved under the live data
# root (``<repo>/data`` by default, ``FIRESTARTER_DATA`` when set).
DRIFT_FILE = SIMULATION_LOC.path / "drift.yaml"

# Generic Nokia banner. No real hostnames or addresses: the "from" address is the
# demo's own management network and "admin" is the bootstrap user every SR OS has.
SOFTWARE_VERSION = "TiMOS-C-25.10.R2"
PLATFORM_NAME = "Nokia 7250 IXR"
MANAGEMENT_SOURCE = "10.0.100.10"

# Statements every real SR OS prints that the renderer deliberately does not
# produce. Two kinds, each handled by a different compliance input:
#
# - the local ``admin`` user: required config that is simply not modelled, so the
#   demo's ``compliance/extra/base.cfg`` declares it as *expected* on every device
#   (the password hash is redacted on both sides before the diff);
# - platform defaults (log, QoS, SSH cipher lists): noise that ``compliance/ignore/
#   base.yaml`` drops, as it does on a real estate.
PLATFORM_LINES: tuple[str, ...] = (
    'configure system security user-params local-user user "admin" access console true',
    'configure system security user-params local-user user "admin" console member '
    '["administrative"]',
    'configure system security user-params local-user user "admin" password '
    '"$2y$10$Gx5sQ0pZk3u7Ee9v1s2hVOq3bC4dE5fG6hI7jK8lM9nO0pQ1rS2tU"',
    'configure system security user-params local-user user "admin" restricted-to-home false',
    'configure log filter "1001" named-entry "10" description "Collect only events of major '
    'severity or higher"',
    'configure log filter "1001" named-entry "10" action forward',
    'configure log filter "1001" named-entry "10" match severity gte major',
    'configure log log-id "99" description "Default system log"',
    'configure log log-id "99" source main true',
    'configure log log-id "99" destination memory max-entries 500',
    'configure log log-id "100" description "Default serious errors log"',
    'configure log log-id "100" filter "1001"',
    'configure log log-id "100" source main true',
    'configure log log-id "100" destination memory max-entries 500',
    'configure qos vlan-qos-policy "default"',
    'configure qos queue-mgmt-policy "default"',
    'configure qos egress-remark-policy "default"',
    "configure system security ssh server-cipher-list-v2 cipher 190 name aes256-ctr",
    "configure system security ssh server-cipher-list-v2 cipher 192 name aes192-ctr",
    "configure system security ssh server-cipher-list-v2 cipher 194 name aes128-ctr",
    "configure system security ssh server-mac-list-v2 mac 200 name hmac-sha2-512",
    "configure system security ssh server-mac-list-v2 mac 210 name hmac-sha2-256",
)


# ---------------------------------------------------------------------------
# Drift file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceDrift:
    """One device's deviations. ``remove`` holds ``(match_type, value)`` pairs."""

    remove: tuple[tuple[str, str], ...] = ()
    replace: tuple[tuple[str, str], ...] = ()
    add: tuple[str, ...] = ()


@dataclass(frozen=True)
class Drift:
    """The parsed drift file: per-device deviations plus the unreachable set."""

    devices: dict[str, DeviceDrift] = field(default_factory=dict)
    unreachable: dict[str, str] = field(default_factory=dict)


_MATCH_TYPES = ("exact", "startswith", "regex")


def _parse_remove_rule(rule: dict) -> tuple[str, str]:
    """``{startswith: '...'}`` → ``("startswith", "...")``; exactly one key allowed."""
    keys = [k for k in _MATCH_TYPES if k in rule]
    if len(keys) != 1:
        raise ValueError(
            f"A drift 'remove' rule needs exactly one of {_MATCH_TYPES}, got {sorted(rule)}."
        )
    return keys[0], str(rule[keys[0]])


def load_drift(*, path: Path | None = None) -> Drift:
    """Parse the drift file at *path* (default :data:`DRIFT_FILE`).

    A missing file is not an error — it is simply "no drift", which is also what a
    freshly cloned demo has until the data root is populated.
    """
    drift_path = DRIFT_FILE if path is None else path
    if not drift_path.exists():
        return Drift()

    data = yaml.safe_load(drift_path.read_text(encoding="utf-8")) or {}

    devices: dict[str, DeviceDrift] = {}
    for hostname, spec in (data.get("devices") or {}).items():
        spec = spec or {}
        devices[hostname] = DeviceDrift(
            remove=tuple(_parse_remove_rule(r) for r in spec.get("remove") or []),
            replace=tuple((str(r["from"]), str(r["to"])) for r in spec.get("replace") or []),
            add=tuple(str(line) for line in spec.get("add") or []),
        )

    unreachable = {str(h): str(msg) for h, msg in (data.get("unreachable") or {}).items()}
    return Drift(devices=devices, unreachable=unreachable)


# ---------------------------------------------------------------------------
# From rendered intent to a device dump
# ---------------------------------------------------------------------------


def _intent_lines(rendered: str) -> list[str]:
    """Rendered text → bare ``configure ...`` statements (no ``/``, no blanks/comments)."""
    out: list[str] = []
    for raw in rendered.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line[1:] if line.startswith("/") else line)
    return out


def _matches(line: str, *, match_type: str, value: str) -> bool:
    if match_type == "exact":
        return line == value
    if match_type == "startswith":
        return line.startswith(value)
    return re.search(value, line) is not None


def apply_drift(lines: list[str], *, drift: DeviceDrift, hostname: str = "") -> list[str]:
    """Apply one device's drift to its bare intent lines, in remove → replace → add order.

    Order matters only in that ``replace`` works on what ``remove`` left, so a line
    cannot be both removed and replaced. ``add`` lines go at the end — a real dump is
    ordered by the device's own schema, but compliance is a set comparison, so the
    position is cosmetic.
    """
    out = list(lines)

    for match_type, value in drift.remove:
        kept = [ln for ln in out if not _matches(ln, match_type=match_type, value=value)]
        if len(kept) == len(out):
            log.warning("drift for %s: remove %s=%r matched no line", hostname, match_type, value)
        out = kept

    for old, new in drift.replace:
        if old not in out:
            log.warning("drift for %s: replace from=%r matched no line", hostname, old)
        out = [new if ln == old else ln for ln in out]

    out.extend(drift.add)
    return out


def _timestamp_for_header(now: datetime) -> str:
    """``2026-10-03T07:42:04.8+00:00`` — the tenth-of-a-second form SR OS prints."""
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 100000}+00:00"


def to_device_dump(*, hostname: str, lines: list[str], now: datetime) -> str:
    """Wrap bare statements in the header, indentation and trailer of a real dump."""
    stamp = _timestamp_for_header(now)
    header = [
        f"# {SOFTWARE_VERSION} cpm/x86hops64 {PLATFORM_NAME} Copyright (c) 2000-2025 Nokia.",
        "# All rights reserved. All use subject to applicable license agreements.",
        "# Built on Wed Dec 17 21:07:16 UTC 2025 by builder in /builds/2510B/R2/panos",
        "# Configuration format version 25.10 revision 0",
        "",
        f"# Generated {stamp} by admin from {MANAGEMENT_SOURCE}",
        f"# Last modified {stamp} by admin (MD-CLI) from {MANAGEMENT_SOURCE}",
        f"# Last saved {stamp} by system from Console",
        "",
    ]
    body = [f"    {line}" for line in lines]
    trailer = ["", f"# Finished {stamp}", ""]
    return "\n".join(header + body + trailer)


def simulate_dump(
    *,
    hostname: str,
    rendered: str,
    drift: Drift,
    now: datetime | None = None,
) -> str:
    """The text a simulated *hostname* returns for ``admin show configuration flat``."""
    lines = _intent_lines(rendered)
    lines = apply_drift(lines, drift=drift.devices.get(hostname, DeviceDrift()), hostname=hostname)
    lines.extend(PLATFORM_LINES)
    return to_device_dump(hostname=hostname, lines=lines, now=now or datetime.now(UTC))


def fetch_config(
    *,
    hostname: str,
    session: Session,
    drift: Drift | None = None,
) -> FetchResult:
    """Fetch one simulated device's configuration — the counterpart of a Netmiko fetch.

    Renders *hostname* via the Printer, reshapes the result into a device dump and
    applies the drift file. A device listed under ``unreachable`` yields a failed
    :class:`FetchResult` (``raw=None``) carrying the configured error, exactly as a
    connection failure would.
    """
    drift = load_drift() if drift is None else drift
    if hostname in drift.unreachable:
        return FetchResult(hostname=hostname, raw=None, error=drift.unreachable[hostname])

    rendered = Printer(session=session).render_device(hostname=hostname)
    return FetchResult(
        hostname=hostname, raw=simulate_dump(hostname=hostname, rendered=rendered, drift=drift)
    )


# ---------------------------------------------------------------------------
# Lifecycle: the simulated routers are "in service"
# ---------------------------------------------------------------------------


def activate_simulated_devices(*, session: Session) -> int:
    """Mark every simulated router ``active``; returns how many changed.

    The pipeline leaves a freshly built topology at ``planned`` and the operator
    pages that walk a device through ``preactivated → reachable → active`` are not
    part of the demo. In the simulation the routers simply *are* live — so the
    Nornir inventory (active devices only) has hosts, and the remediation specs'
    ``status: active`` gate can pass. CEs are left alone: they are managed through
    their PE, have no rendered config and no inventory entry, so their lifecycle
    state is not the simulation's business. Idempotent.
    """
    ce_hostnames = set(find_ce_lags(session))
    changed = 0
    for device in session.execute(select(Device)).scalars():
        if device.hostname in ce_hostnames or device.status == DeviceStatus.retired:
            continue
        if device.status != DeviceStatus.active:
            device.status = DeviceStatus.active
            changed += 1
    session.flush()
    return changed


# ---------------------------------------------------------------------------
# A whole backup run
# ---------------------------------------------------------------------------


def run_simulated_backup(
    *,
    session: Session,
    backups_dir: Path = BACKUPS_DIR,
    drift_file: Path | None = None,
) -> dict[str, FetchResult]:
    """Back up every simulated device and record the run like ``run_backup`` does.

    Writes ``latest/<host>.cfg``, ``fetch_results.json`` and ``datetime.txt`` through
    the same helpers as :func:`app.automation.backup.run_backup`, so the compliance
    runner and the Latest-backups page read it without knowing it was simulated. The
    device set is every device the Printer renders (CE switches have no rendered
    config and are therefore absent, as they would be from a Nornir inventory of
    routers). Unreachable devices get a result but no ``.cfg`` — the same rule
    ``write_run`` applies to a real failed fetch.
    """
    drift = load_drift(path=drift_file)
    rendered = Printer(session=session).print_all()
    now = datetime.now(UTC)

    results: dict[str, FetchResult] = {}
    for hostname, text in rendered.items():
        if hostname in drift.unreachable:
            results[hostname] = FetchResult(
                hostname=hostname, raw=None, error=drift.unreachable[hostname]
            )
            continue
        results[hostname] = FetchResult(
            hostname=hostname,
            raw=simulate_dump(hostname=hostname, rendered=text, drift=drift, now=now),
        )

    timestamp = now.strftime(TIMESTAMP_FORMAT)
    archive_latest(backups_dir=backups_dir)
    write_run(results=results, timestamp=timestamp, backups_dir=backups_dir)
    prune_old_backups(backups_dir=backups_dir)
    return results
