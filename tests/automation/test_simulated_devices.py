"""Tests for the simulated device layer (``app.automation.simulated_devices``).

The Printer is stubbed with a small canned rendering so no topology is needed; what
is under test is the drift application, the device-dump shape (it must normalise to
exactly the rendered intent, so a drift-free backup yields zero findings) and the
on-disk layout, which must be what ``backup.load_results`` expects.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar
from unittest.mock import patch

import pytest

from app.automation import simulated_devices as sim
from app.automation.backup import FetchResult, load_results, read_timestamp
from app.compliance.differ import ComplianceDiffer
from app.compliance.normaliser import ComplianceNormaliser

RENDERED = """\
/configure system name "pe1.SiteX"
/configure port 1/1/c1/1 admin-state enable
/configure port 1/1/c1/1 ethernet mtu 9212

/configure lag "lag-2" description "Remote: pe1.SiteY:lag-2"
/configure router "Base" bgp neighbor "10.0.0.2" admin-state enable
/configure router "Base" bgp neighbor "10.0.0.2" group "RR"
/configure router "Base" isis 0 interface "lag-2" hello-authentication-keychain "ISIS-KEYCHAIN"
"""

INTENT = [
    'configure system name "pe1.SiteX"',
    "configure port 1/1/c1/1 admin-state enable",
    "configure port 1/1/c1/1 ethernet mtu 9212",
    'configure lag "lag-2" description "Remote: pe1.SiteY:lag-2"',
    'configure router "Base" bgp neighbor "10.0.0.2" admin-state enable',
    'configure router "Base" bgp neighbor "10.0.0.2" group "RR"',
    'configure router "Base" isis 0 interface "lag-2" hello-authentication-keychain '
    '"ISIS-KEYCHAIN"',
]

DRIFT_YAML = """\
devices:
  pe1.SiteX:
    remove:
      - startswith: 'configure router "Base" bgp neighbor "10.0.0.2"'
      - regex: 'hello-authentication-keychain'
    replace:
      - from: 'configure port 1/1/c1/1 admin-state enable'
        to:   'configure port 1/1/c1/1 admin-state disable'
    add:
      - 'configure router "Base" static-routes route 192.168.99.0/24 route-type unicast next-hop "10.0.4.1" admin-state enable'
unreachable:
  pe1.SiteZ: "TCP connection to device failed."
"""

NOW = datetime(2026, 10, 3, 7, 42, 4, 812345, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Drift file parsing and application
# ---------------------------------------------------------------------------


class TestLoadDrift:
    def test_missing_file_means_no_drift(self, tmp_path):
        drift = sim.load_drift(path=tmp_path / "nope.yaml")
        assert drift == sim.Drift()

    def test_parses_all_sections(self, tmp_path):
        path = tmp_path / "drift.yaml"
        path.write_text(DRIFT_YAML, encoding="utf-8")
        drift = sim.load_drift(path=path)

        dev = drift.devices["pe1.SiteX"]
        assert dev.remove == (
            ("startswith", 'configure router "Base" bgp neighbor "10.0.0.2"'),
            ("regex", "hello-authentication-keychain"),
        )
        assert dev.replace == (
            (
                "configure port 1/1/c1/1 admin-state enable",
                "configure port 1/1/c1/1 admin-state disable",
            ),
        )
        assert len(dev.add) == 1
        assert drift.unreachable == {"pe1.SiteZ": "TCP connection to device failed."}

    def test_empty_device_entry_is_allowed(self, tmp_path):
        path = tmp_path / "drift.yaml"
        path.write_text("devices:\n  pe1.SiteX:\nunreachable:\n", encoding="utf-8")
        drift = sim.load_drift(path=path)
        assert drift.devices["pe1.SiteX"] == sim.DeviceDrift()

    def test_remove_rule_needs_exactly_one_match_key(self, tmp_path):
        path = tmp_path / "drift.yaml"
        path.write_text(
            "devices:\n  h:\n    remove:\n      - startswith: a\n        exact: b\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="exactly one"):
            sim.load_drift(path=path)

    def test_default_path_is_the_module_constant(self, tmp_path, monkeypatch):
        path = tmp_path / "drift.yaml"
        path.write_text("unreachable:\n  h1: down\n", encoding="utf-8")
        monkeypatch.setattr(sim, "DRIFT_FILE", path)
        assert sim.load_drift().unreachable == {"h1": "down"}


class TestApplyDrift:
    def test_remove_startswith_drops_the_whole_subtree(self):
        drift = sim.DeviceDrift(
            remove=(("startswith", 'configure router "Base" bgp neighbor "10.0.0.2"'),)
        )
        out = sim.apply_drift(INTENT, drift=drift)
        assert not any("10.0.0.2" in ln for ln in out)
        assert len(out) == len(INTENT) - 2

    def test_remove_exact_and_regex(self):
        drift = sim.DeviceDrift(
            remove=(
                ("exact", 'configure system name "pe1.SiteX"'),
                ("regex", r"isis 0 interface \S+ hello-authentication"),
            )
        )
        out = sim.apply_drift(INTENT, drift=drift)
        assert 'configure system name "pe1.SiteX"' not in out
        assert not any("hello-authentication" in ln for ln in out)
        assert len(out) == len(INTENT) - 2

    def test_replace_swaps_one_line_in_place(self):
        drift = sim.DeviceDrift(
            replace=(
                (
                    "configure port 1/1/c1/1 admin-state enable",
                    "configure port 1/1/c1/1 admin-state disable",
                ),
            )
        )
        out = sim.apply_drift(INTENT, drift=drift)
        assert out[1] == "configure port 1/1/c1/1 admin-state disable"
        assert len(out) == len(INTENT)

    def test_add_appends_new_lines(self):
        drift = sim.DeviceDrift(add=("configure extra one", "configure extra two"))
        out = sim.apply_drift(INTENT, drift=drift)
        assert out[-2:] == ["configure extra one", "configure extra two"]

    def test_no_drift_is_identity(self):
        assert sim.apply_drift(INTENT, drift=sim.DeviceDrift()) == INTENT

    def test_unmatched_rules_warn_rather_than_raise(self, caplog):
        drift = sim.DeviceDrift(
            remove=(("exact", "configure nothing like this"),),
            replace=(("configure also absent", "configure x"),),
        )
        with caplog.at_level("WARNING", logger=sim.__name__):
            out = sim.apply_drift(INTENT, drift=drift, hostname="h1")
        assert out == INTENT
        assert "matched no line" in caplog.text
        assert caplog.text.count("h1") == 2


# ---------------------------------------------------------------------------
# Device dump shape
# ---------------------------------------------------------------------------


class TestDeviceDump:
    def test_has_nokia_header_indented_body_and_trailer(self):
        text = sim.to_device_dump(hostname="h", lines=['configure system name "h"'], now=NOW)
        lines = text.splitlines()
        assert lines[0].startswith("# TiMOS-C-25.10.R2 ")
        assert "# Generated 2026-10-03T07:42:04.8+00:00 by admin from 10.0.100.10" in lines
        assert '    configure system name "h"' in lines
        assert lines[-1] == "# Finished 2026-10-03T07:42:04.8+00:00"
        # Only the demo's own management source address appears in the header.
        assert "10.0.100.10" in text

    def test_intent_lines_strip_slash_blanks_and_comments(self):
        assert sim._intent_lines("# c\n\n/configure a\nconfigure b\n") == [
            "configure a",
            "configure b",
        ]

    def test_dump_contains_platform_lines(self):
        text = sim.simulate_dump(hostname="h", rendered=RENDERED, drift=sim.Drift(), now=NOW)
        for line in sim.PLATFORM_LINES:
            assert f"    {line}" in text.splitlines()


# ---------------------------------------------------------------------------
# Compliance equivalence: no drift → no findings
# ---------------------------------------------------------------------------


@pytest.fixture
def compliance_dirs(tmp_path, monkeypatch):
    """Demo-style ignore/extra inputs: platform defaults ignored, admin user expected."""
    ignore = tmp_path / "ignore"
    ignore.mkdir()
    (ignore / "base.yaml").write_text(
        "ignore:\n"
        '  - match: startswith\n    value: "configure log "\n'
        '  - match: startswith\n    value: "configure qos "\n'
        '  - match: startswith\n    value: "configure system security ssh server-"\n'
        "redact:\n"
        '  - match: regex\n    value: "^(configure system security user-params .* password) .*$"\n'
        '    replace: "\\\\1 <redacted>"\n',
        encoding="utf-8",
    )
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "base.cfg").write_text(
        "\n".join(
            line if "password" not in line else line.split(" password ")[0] + " password <redacted>"
            for line in sim.PLATFORM_LINES
            if 'user "admin"' in line
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.compliance.normaliser.IGNORE_DIR", ignore)
    monkeypatch.setattr("app.compliance.normaliser.BASE_IGNORE_FILE", ignore / "base.yaml")
    monkeypatch.setattr("app.compliance.normaliser.EXTRA_DIR", extra)


def _diff(session, *, rendered: str, live: str):
    with (
        patch("app.compliance.normaliser.get_device_model_by_hostname", return_value="7250-IXR"),
        patch("app.compliance.normaliser.get_device_role_by_hostname", return_value="pe"),
    ):
        n = ComplianceNormaliser(session=session)
        r = n.normalise_rendered(hostname="pe1.SiteX", raw=rendered)
        live_norm = n.normalise(hostname="pe1.SiteX", raw=live)
    return ComplianceDiffer().diff(rendered=r, live=live_norm)


class TestComplianceEquivalence:
    def test_drift_free_dump_yields_zero_findings(self, session, compliance_dirs):
        live = sim.simulate_dump(hostname="pe1.SiteX", rendered=RENDERED, drift=sim.Drift())
        result = _diff(session, rendered=RENDERED, live=live)
        assert result.compliant, (result.only_in_rendered, result.only_in_live)

    def test_findings_are_exactly_the_planted_drift(self, session, compliance_dirs, tmp_path):
        path = tmp_path / "drift.yaml"
        path.write_text(DRIFT_YAML, encoding="utf-8")
        drift = sim.load_drift(path=path)

        live = sim.simulate_dump(hostname="pe1.SiteX", rendered=RENDERED, drift=drift)
        result = _diff(session, rendered=RENDERED, live=live)

        assert result.only_in_rendered == frozenset(
            {
                "configure port 1/1/c1/1 admin-state enable",
                'configure router "Base" bgp neighbor "10.0.0.2" admin-state enable',
                'configure router "Base" bgp neighbor "10.0.0.2" group "RR"',
                'configure router "Base" isis 0 interface "lag-2" hello-authentication-keychain '
                '"ISIS-KEYCHAIN"',
            }
        )
        assert result.only_in_live == frozenset(
            {
                "configure port 1/1/c1/1 admin-state disable",
                'configure router "Base" static-routes route 192.168.99.0/24 route-type unicast '
                'next-hop "10.0.4.1" admin-state enable',
            }
        )


# ---------------------------------------------------------------------------
# fetch_config and run_simulated_backup
# ---------------------------------------------------------------------------


class _FakePrinter:
    """Stands in for ``Printer``: two rendered routers, nothing else."""

    renders: ClassVar[dict[str, str]] = {
        "pe1.SiteX": RENDERED,
        "pe1.SiteZ": RENDERED.replace("SiteX", "SiteZ"),
    }

    def __init__(self, *, session, **_):
        self.session = session

    def render_device(self, *, hostname):
        return self.renders[hostname]

    def print_all(self):
        return dict(self.renders)


@pytest.fixture
def fake_printer(monkeypatch):
    monkeypatch.setattr(sim, "Printer", _FakePrinter)


@pytest.fixture
def drift_file(tmp_path, monkeypatch):
    path = tmp_path / "drift.yaml"
    path.write_text(DRIFT_YAML, encoding="utf-8")
    monkeypatch.setattr(sim, "DRIFT_FILE", path)
    return path


class TestFetchConfig:
    def test_reachable_device_returns_a_dump(self, session, fake_printer, drift_file):
        result = sim.fetch_config(hostname="pe1.SiteX", session=session)
        assert result.ok
        assert result.raw is not None
        assert "# TiMOS-C" in result.raw
        assert "    configure port 1/1/c1/1 admin-state disable" in result.raw

    def test_unreachable_device_fails_like_a_connection_error(
        self, session, fake_printer, drift_file
    ):
        result = sim.fetch_config(hostname="pe1.SiteZ", session=session)
        assert result == FetchResult(
            hostname="pe1.SiteZ", raw=None, error="TCP connection to device failed."
        )


class TestRunSimulatedBackup:
    def test_layout_round_trips_through_backup_load_results(
        self, session, fake_printer, drift_file, tmp_path
    ):
        backups = tmp_path / "backups"
        results = sim.run_simulated_backup(session=session, backups_dir=backups)

        latest = backups / "latest"
        assert sorted(p.name for p in latest.iterdir()) == [
            "datetime.txt",
            "fetch_results.json",
            "pe1.SiteX.cfg",  # the unreachable device gets no .cfg, as in backup.write_run
        ]
        payload = json.loads((latest / "fetch_results.json").read_text(encoding="utf-8"))
        assert payload["results"] == {
            "pe1.SiteX": {"ok": True, "error": None},
            "pe1.SiteZ": {"ok": False, "error": "TCP connection to device failed."},
        }
        assert payload["timestamp"] == read_timestamp(backups_dir=backups)

        # What backup.py reads back is exactly what the run returned.
        loaded = load_results(backups_dir=backups)
        assert loaded == results
        assert loaded["pe1.SiteX"].raw is not None
        assert loaded["pe1.SiteZ"].raw is None

    def test_rerun_archives_the_previous_latest(self, session, fake_printer, drift_file, tmp_path):
        backups = tmp_path / "backups"
        sim.run_simulated_backup(session=session, backups_dir=backups)
        sim.run_simulated_backup(session=session, backups_dir=backups)
        dirs = sorted(p.name for p in backups.iterdir() if p.is_dir())
        assert "latest" in dirs
        assert len(dirs) == 2  # one archive named after the first run's timestamp

    def test_explicit_drift_file_overrides_the_default(
        self, session, fake_printer, drift_file, tmp_path
    ):
        other = tmp_path / "other.yaml"
        other.write_text("unreachable:\n  pe1.SiteX: 'Authentication failed.'\n", encoding="utf-8")
        results = sim.run_simulated_backup(
            session=session, backups_dir=tmp_path / "b", drift_file=other
        )
        assert results["pe1.SiteX"].error == "Authentication failed."
        assert results["pe1.SiteZ"].ok


# ---------------------------------------------------------------------------
# activate_simulated_devices
# ---------------------------------------------------------------------------


class TestActivateSimulatedDevices:
    def test_routers_become_active_ces_stay_and_rerun_is_a_noop(self, session, seeded_topology):
        from sqlalchemy import select

        from app.models import Device, DeviceStatus
        from app.services.service_handling.ce_lags import find_ce_lags

        ces = set(find_ce_lags(session))
        assert ces, "the test workbook seeds at least one CE"

        changed = sim.activate_simulated_devices(session=session)
        devices = session.execute(select(Device)).scalars().all()
        routers = [d for d in devices if d.hostname not in ces]
        assert changed == len(routers)
        assert all(d.status == DeviceStatus.active for d in routers)
        assert all(d.status != DeviceStatus.active for d in devices if d.hostname in ces)

        assert sim.activate_simulated_devices(session=session) == 0
