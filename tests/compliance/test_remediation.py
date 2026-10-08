"""Tests for the compliance remediation projection (ADR 0004).

The projection turns a compliance run's per-device diff into gated candidates: the
``add`` rules derive additive lines from ``only_in_rendered`` ("missing from
device"), and the ``delete`` rules emit explicit ``remediation_commands`` when they match
``only_in_live`` ("unexpected on device"). These tests own their spec files under
``tmp_path`` and build devices directly, so nothing depends on environment data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.compliance.differ import DiffResult
from app.compliance.remediation import (
    RemediationSpec,
    evaluate_conditions,
    load_remediation_spec,
    project_remediation,
)
from app.models import Device, DeviceStatus, Role, Site

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _write_spec(remediation_dir: Path, role_name: str, body: str) -> None:
    remediation_dir.mkdir(parents=True, exist_ok=True)
    (remediation_dir / f"{role_name}.yaml").write_text(body, encoding="utf-8")


def _write_base(remediation_dir: Path, body: str) -> None:
    """Write the role-independent ``base.yaml`` layer."""
    remediation_dir.mkdir(parents=True, exist_ok=True)
    (remediation_dir / "base.yaml").write_text(body, encoding="utf-8")


def _device(
    session,
    *,
    hostname: str,
    role_name: str,
    status: DeviceStatus = DeviceStatus.active,
    labels: dict[str, str] | None = None,
) -> Device:
    site = Site(name=f"site-{hostname}")
    role = session.scalar(select(Role).where(Role.name == role_name))
    if role is None:
        role = Role(name=role_name)
        session.add(role)
    session.add(site)
    session.flush()
    device = Device(
        hostname=hostname,
        lag_name="lag",
        model_name="test_model_1",
        labels=labels or {},
        status=status,
        site=site,
        role=role,
    )
    session.add(device)
    session.flush()
    return device


def _diff(
    *, only_in_rendered: set[str] | None = None, only_in_live: set[str] | None = None
) -> DiffResult:
    return DiffResult(
        hostname="host",
        only_in_rendered=frozenset(only_in_rendered or set()),
        only_in_live=frozenset(only_in_live or set()),
        ignored_rendered=0,
        ignored_live=0,
    )


# ---------------------------------------------------------------------------
# load_remediation_spec
# ---------------------------------------------------------------------------


class TestLoadRemediationSpec:
    def test_absent_file_returns_none(self, tmp_path):
        assert load_remediation_spec("pe", remediation_dir=tmp_path) is None

    def test_add_rules_compile_to_matchers(self, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            """
            add:
              - match: startswith
                value: "configure router \\"Base\\""
              - match: exact
                value: "configure system name \\"x\\""
            """,
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        assert any(rule('configure router "Base" autonomous-system 1') for rule in spec.add)
        assert any(rule('configure system name "x"') for rule in spec.add)
        assert not any(rule("configure log log-id 99") for rule in spec.add)

    def test_delete_rules_compile_and_carry_remediation_commands(self, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure router \\"Base\\" bgp neighbor \\"192.0.2.17\\""\n'
            "    remediation_commands:\n"
            '      - \'delete router "Base" bgp neighbor "192.0.2.17"\'\n',
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        assert any(
            rule('configure router "Base" bgp neighbor "192.0.2.17" admin-state enable')
            for rule in spec.delete
        )
        assert spec.delete_remediation_commands == (
            ('delete router "Base" bgp neighbor "192.0.2.17"',),
        )

    def test_conditions_parsed(self, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            """
            conditions:
              status: active
              labels:
                admin_password_set: "True"
            add:
              - match: startswith
                value: "x"
            """,
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec.required_status == "active"
        assert spec.required_labels == {"admin_password_set": "True"}

    def test_missing_conditions_defaults_to_no_gates(self, tmp_path):
        _write_spec(tmp_path, "pe", "add:\n  - match: startswith\n    value: x\n")
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec.required_status is None
        assert spec.required_labels == {}

    def test_empty_add_and_delete_is_inert(self, tmp_path):
        _write_spec(tmp_path, "pe", "conditions:\n  status: active\n")
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        assert spec.add == []
        assert spec.delete == []
        assert spec.delete_remediation_commands == ()

    def test_unknown_add_match_type_raises(self, tmp_path):
        _write_spec(tmp_path, "pe", "add:\n  - match: contains\n    value: x\n")
        with pytest.raises(ValueError, match="match type"):
            load_remediation_spec("pe", remediation_dir=tmp_path)

    def test_unknown_delete_match_type_raises(self, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n  - match: contains\n    value: x\n    remediation_commands:\n      - 'delete x'\n",
        )
        with pytest.raises(ValueError, match="match type"):
            load_remediation_spec("pe", remediation_dir=tmp_path)


# ---------------------------------------------------------------------------
# load_remediation_spec — the base.yaml layer
# ---------------------------------------------------------------------------


class TestBaseLayer:
    def test_base_only_returns_spec(self, tmp_path):
        """base.yaml alone (no role file) makes the role remediable."""
        _write_base(tmp_path, 'add:\n  - match: startswith\n    value: "configure system"\n')
        spec = load_remediation_spec("core", remediation_dir=tmp_path)
        assert spec is not None
        assert any(rule('configure system name "core1"') for rule in spec.add)

    def test_neither_file_returns_none(self, tmp_path):
        assert load_remediation_spec("core", remediation_dir=tmp_path) is None

    def test_base_and_role_add_rules_merge(self, tmp_path):
        _write_base(tmp_path, 'add:\n  - match: startswith\n    value: "configure system"\n')
        _write_spec(tmp_path, "pe", 'add:\n  - match: startswith\n    value: "configure router"\n')
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        # Both layers' rules are present.
        assert any(rule('configure system name "x"') for rule in spec.add)
        assert any(rule("configure router bgp") for rule in spec.add)
        # Base rules come first (coarse → fine).
        assert len(spec.add) == 2

    def test_base_and_role_delete_rules_merge_commands_aligned(self, tmp_path):
        _write_base(
            tmp_path,
            "delete:\n"
            '  - match: startswith\n    value: "configure log log-id \\"base\\""\n'
            "    remediation_commands:\n      - 'delete log log-id \"base\"'\n",
        )
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            '  - match: startswith\n    value: "configure router \\"Base\\" bgp neighbor \\"192.0.2.17\\""\n'
            '    remediation_commands:\n      - \'delete router "Base" bgp neighbor "192.0.2.17"\'\n',
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        # Base delete matcher is first, role's second, and commands stay index-aligned.
        assert spec.delete_remediation_commands == (
            ('delete log log-id "base"',),
            ('delete router "Base" bgp neighbor "192.0.2.17"',),
        )
        assert spec.delete[0].match('configure log log-id "base" name "x"') is not None
        assert (
            spec.delete[1].match(
                'configure router "Base" bgp neighbor "192.0.2.17" admin-state enable'
            )
            is not None
        )

    def test_role_status_overrides_base(self, tmp_path):
        _write_base(tmp_path, "conditions:\n  status: reachable\n")
        _write_spec(
            tmp_path,
            "pe",
            "conditions:\n  status: active\nadd:\n  - match: startswith\n    value: x\n",
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        assert spec.required_status == "active"

    def test_base_status_applies_when_role_has_none(self, tmp_path):
        _write_base(tmp_path, "conditions:\n  status: active\n")
        _write_spec(tmp_path, "pe", "add:\n  - match: startswith\n    value: x\n")
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        assert spec is not None
        assert spec.required_status == "active"

    def test_labels_union_with_role_winning(self, tmp_path):
        _write_base(tmp_path, 'conditions:\n  labels:\n    ring: "core"\n    zone: "base"\n')
        _write_spec(
            tmp_path,
            "pe",
            'conditions:\n  labels:\n    zone: "role"\nadd:\n  - match: startswith\n    value: x\n',
        )
        spec = load_remediation_spec("pe", remediation_dir=tmp_path)
        # Base-only key kept, shared key taken from the role.
        assert spec is not None
        assert spec.required_labels == {"ring": "core", "zone": "role"}

    def test_empty_base_is_inert_but_still_a_layer(self, tmp_path):
        """A present-but-empty base.yaml makes a role's spec load without adding rules."""
        _write_base(tmp_path, "")
        spec = load_remediation_spec("core", remediation_dir=tmp_path)
        assert spec is not None
        assert spec.add == []
        assert spec.delete == []
        assert spec.delete_remediation_commands == ()

    def test_base_applies_to_role_without_own_file(self, session, tmp_path):
        """A device whose role has no <role>.yaml is still remediated by base.yaml."""
        _write_base(tmp_path, 'add:\n  - match: startswith\n    value: "configure system"\n')
        _device(session, hostname="core1", role_name="core", status=DeviceStatus.active)
        results = {
            "core1": _diff(
                only_in_rendered={'configure system name "core1"', "configure log log-id 99"}
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert len(out) == 1
        assert out[0].lines == ('configure system name "core1"',)


# ---------------------------------------------------------------------------
# evaluate_conditions
# ---------------------------------------------------------------------------


class TestEvaluateConditions:
    def _spec(self, *, status=None, labels=None) -> RemediationSpec:
        return RemediationSpec(
            role_name="pe",
            add=[],
            delete=[],
            delete_remediation_commands=(),
            required_status=status,
            required_labels=labels or {},
        )

    def test_no_conditions_always_eligible(self, session):
        device = _device(session, hostname="d1", role_name="pe", status=DeviceStatus.planned)
        eligible, reasons = evaluate_conditions(device=device, spec=self._spec())
        assert eligible is True
        assert reasons == ()

    def test_status_match_is_eligible(self, session):
        device = _device(session, hostname="d1", role_name="pe", status=DeviceStatus.active)
        eligible, _reasons = evaluate_conditions(device=device, spec=self._spec(status="active"))
        assert eligible is True

    def test_status_mismatch_blocks_with_reason(self, session):
        device = _device(session, hostname="d1", role_name="pe", status=DeviceStatus.planned)
        eligible, reasons = evaluate_conditions(device=device, spec=self._spec(status="active"))
        assert eligible is False
        assert "status is planned" in reasons[0]
        assert "needs active" in reasons[0]

    def test_missing_label_blocks(self, session):
        device = _device(session, hostname="d1", role_name="pe", labels={})
        eligible, reasons = evaluate_conditions(
            device=device, spec=self._spec(labels={"admin_password_set": "True"})
        )
        assert eligible is False
        assert "admin_password_set" in reasons[0]

    def test_label_value_mismatch_blocks(self, session):
        device = _device(
            session, hostname="d1", role_name="pe", labels={"admin_password_set": "False"}
        )
        eligible, _reasons = evaluate_conditions(
            device=device, spec=self._spec(labels={"admin_password_set": "True"})
        )
        assert eligible is False

    def test_all_conditions_met_is_eligible(self, session):
        device = _device(
            session,
            hostname="d1",
            role_name="pe",
            status=DeviceStatus.active,
            labels={"admin_password_set": "True"},
        )
        eligible, reasons = evaluate_conditions(
            device=device,
            spec=self._spec(status="active", labels={"admin_password_set": "True"}),
        )
        assert eligible is True
        assert reasons == ()


# ---------------------------------------------------------------------------
# project_remediation — the additive half (`add` over only_in_rendered)
# ---------------------------------------------------------------------------


class TestProjectAdd:
    def test_add_listed_lines_only(self, session, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            'add:\n  - match: startswith\n    value: "configure router"\n',
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {
            "pe1": _diff(
                only_in_rendered={
                    "configure router isis 0",
                    "configure router bgp",
                    "configure log log-id 99",  # not add-listed
                }
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert len(out) == 1
        assert out[0].lines == ("configure router bgp", "configure router isis 0")
        assert out[0].remediation_commands == ()

    def test_eligible_when_conditions_met(self, session, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            'conditions:\n  status: active\nadd:\n  - match: startswith\n    value: "x"\n',
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_rendered={"x 1"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].eligible is True
        assert out[0].blocked_reasons == ()

    def test_blocked_device_still_reports_lines(self, session, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            'conditions:\n  status: active\nadd:\n  - match: startswith\n    value: "x"\n',
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.planned)
        results = {"pe1": _diff(only_in_rendered={"x 1"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert len(out) == 1
        assert out[0].eligible is False
        assert out[0].lines == ("x 1",)
        assert "status is planned" in out[0].blocked_reasons[0]

    def test_device_without_matching_lines_is_omitted(self, session, tmp_path):
        _write_spec(tmp_path, "pe", 'add:\n  - match: startswith\n    value: "never"\n')
        _device(session, hostname="pe1", role_name="pe")
        results = {"pe1": _diff(only_in_rendered={"configure router bgp"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []

    def test_role_without_spec_is_omitted(self, session, tmp_path):
        _device(session, hostname="core1", role_name="core")
        results = {"core1": _diff(only_in_rendered={"configure router bgp"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []

    def test_compliant_device_is_omitted(self, session, tmp_path):
        _write_spec(tmp_path, "pe", 'add:\n  - match: startswith\n    value: "x"\n')
        _device(session, hostname="pe1", role_name="pe")
        results = {"pe1": _diff()}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []

    def test_output_sorted_by_hostname(self, session, tmp_path):
        _write_spec(tmp_path, "pe", 'add:\n  - match: startswith\n    value: "x"\n')
        _device(session, hostname="pe2", role_name="pe")
        _device(session, hostname="pe1", role_name="pe")
        results = {
            "pe2": _diff(only_in_rendered={"x 2"}),
            "pe1": _diff(only_in_rendered={"x 1"}),
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert [d.hostname for d in out] == ["pe1", "pe2"]

    def test_empty_spec_remediates_nothing(self, session, tmp_path):
        _write_spec(tmp_path, "pe", "conditions:\n  status: active\n")
        _device(session, hostname="pe1", role_name="pe")
        results = {"pe1": _diff(only_in_rendered={"configure router bgp"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []


# ---------------------------------------------------------------------------
# project_remediation — the removal half (`delete` over only_in_live)
# ---------------------------------------------------------------------------


# The concrete BGP RR migration: `add` passes the NEW peer's missing lines through,
# and `delete` emits the explicit removal when the OLD peer is still on the device.
_RR_SPEC = (
    "add:\n"
    "  - match: startswith\n"
    '    value: "configure router \\"Base\\" bgp neighbor \\"192.0.2.1\\""\n'
    "delete:\n"
    "  - match: startswith\n"
    '    value: "configure router \\"Base\\" bgp neighbor \\"192.0.2.17\\""\n'
    "    remediation_commands:\n"
    '      - \'delete router "Base" bgp neighbor "192.0.2.17"\'\n'
)


class TestProjectDelete:
    def test_delete_fires_on_unexpected_line(self, session, tmp_path):
        _write_spec(tmp_path, "pe", _RR_SPEC)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {
            "pe1": _diff(
                only_in_rendered={
                    'configure router "Base" bgp neighbor "192.0.2.1" admin-state enable',
                },
                only_in_live={
                    'configure router "Base" bgp neighbor "192.0.2.17" admin-state enable',
                    'configure router "Base" bgp neighbor "192.0.2.17" group "CORE_RR"',
                },
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert len(out) == 1
        # additive line derived from only_in_rendered …
        assert out[0].lines == (
            'configure router "Base" bgp neighbor "192.0.2.1" admin-state enable',
        )
        # … and the explicit delete, fired by the only_in_live match.
        assert out[0].remediation_commands == ('delete router "Base" bgp neighbor "192.0.2.17"',)

    def test_delete_only_device_is_reported(self, session, tmp_path):
        # No additions at all — a purely stale-config removal must still surface.
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure system snmp"\n'
            "    remediation_commands:\n"
            "      - 'delete system snmp'\n",
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_live={"configure system snmp community X"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert len(out) == 1
        assert out[0].lines == ()
        assert out[0].remediation_commands == ("delete system snmp",)

    def test_delete_does_not_fire_without_a_live_match(self, session, tmp_path):
        _write_spec(tmp_path, "pe", _RR_SPEC)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        # New peer missing (add fires) but the old peer is NOT on the device.
        results = {
            "pe1": _diff(
                only_in_rendered={
                    'configure router "Base" bgp neighbor "192.0.2.1" admin-state enable',
                },
                only_in_live={"configure log log-id 5 unrelated"},
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].lines == (
            'configure router "Base" bgp neighbor "192.0.2.1" admin-state enable',
        )
        assert out[0].remediation_commands == ()

    def test_delete_matches_only_live_not_rendered(self, session, tmp_path):
        # A delete rule whose value happens to sit in only_in_rendered must NOT fire —
        # deletes read the live side exclusively.
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure router bgp"\n'
            "    remediation_commands:\n"
            "      - 'delete router bgp'\n",
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_rendered={"configure router bgp neighbor x"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []

    def test_remediation_commands_deduplicated_preserving_order(self, session, tmp_path):
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure a"\n'
            "    remediation_commands:\n"
            "      - 'delete old'\n"
            "      - 'delete shared'\n"
            "  - match: startswith\n"
            '    value: "configure b"\n'
            "    remediation_commands:\n"
            "      - 'delete shared'\n"
            "      - 'delete other'\n",
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_live={"configure a 1", "configure b 1"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].remediation_commands == ("delete old", "delete shared", "delete other")

    def test_line_count_includes_both_halves(self, session, tmp_path):
        _write_spec(tmp_path, "pe", _RR_SPEC)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {
            "pe1": _diff(
                only_in_rendered={
                    'configure router "Base" bgp neighbor "192.0.2.1" admin-state enable',
                },
                only_in_live={
                    'configure router "Base" bgp neighbor "192.0.2.17" admin-state enable',
                },
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].line_count == 2  # 1 add + 1 delete

    def test_blocked_device_still_carries_deletes(self, session, tmp_path):
        spec = "conditions:\n  status: active\n" + _RR_SPEC
        _write_spec(tmp_path, "pe", spec)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.planned)
        results = {
            "pe1": _diff(
                only_in_live={
                    'configure router "Base" bgp neighbor "192.0.2.17" admin-state enable',
                }
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].eligible is False
        assert out[0].remediation_commands == ('delete router "Base" bgp neighbor "192.0.2.17"',)

    def test_delete_rule_without_remediation_commands_fires_nothing(self, session, tmp_path):
        # The engine only emits remediation_commands; a delete rule with none is inert (the web
        # validator rejects authoring one, but the engine must not crash on it).
        _write_spec(tmp_path, "pe", 'delete:\n  - match: startswith\n    value: "configure x"\n')
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_live={"configure x 1"})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out == []


# ---------------------------------------------------------------------------
# project_remediation — capture-group substitution into delete commands
# ---------------------------------------------------------------------------


# The concrete DHCP option-42 case: the pool's subnet varies per device, so the delete
# command captures it from the matched "unexpected on device" line via a named group and
# substitutes it into an otherwise-fixed command (the ipv4-address stays hardcoded).
_OPT42_SPEC = (
    "delete:\n"
    "  - match: regex\n"
    "    value: 'subnet (?P<subnet>\\S+) options option 42 ipv4-address \\[10\\.89\\.70\\.172\\]'\n"
    "    remediation_commands:\n"
    '      - \'/configure delete service vprn "CE-DHCP-100" dhcp-server dhcpv4'
    ' "dhcp-server" pool "ce_pool" subnet {subnet} options option 42'
    " ipv4-address 10.89.70.172'\n"
)


def _opt42_live(subnet: str) -> str:
    return (
        'configure service vprn "CE-DHCP-100" dhcp-server dhcpv4 "dhcp-server" '
        f'pool "ce_pool" subnet {subnet} options option 42 ipv4-address [10.89.70.172]'
    )


def _opt42_delete(subnet: str) -> str:
    return (
        '/configure delete service vprn "CE-DHCP-100" dhcp-server dhcpv4 '
        f'"dhcp-server" pool "ce_pool" subnet {subnet} options option 42 '
        "ipv4-address 10.89.70.172"
    )


class TestProjectDeleteSubstitution:
    def test_named_group_substituted_into_command(self, session, tmp_path):
        _write_spec(tmp_path, "pe", _OPT42_SPEC)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_live={_opt42_live("10.102.32.0/28")})}

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].remediation_commands == (_opt42_delete("10.102.32.0/28"),)

    def test_one_command_per_matched_line_sorted(self, session, tmp_path):
        # Two distinct subnets → two delete commands, in deterministic (sorted-line) order
        # regardless of the frozenset's iteration order.
        _write_spec(tmp_path, "pe", _OPT42_SPEC)
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {
            "pe1": _diff(
                only_in_live={
                    _opt42_live("10.102.48.0/28"),
                    _opt42_live("10.102.32.0/28"),
                }
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        # Sorted by the matched line, so 10.102.32 precedes 10.102.48.
        assert out[0].remediation_commands == (
            _opt42_delete("10.102.32.0/28"),
            _opt42_delete("10.102.48.0/28"),
        )

    def test_command_without_placeholder_collapses_across_lines(self, session, tmp_path):
        # A fixed command (no placeholder) matching several lines emits once, as before.
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure system snmp"\n'
            "    remediation_commands:\n"
            "      - 'delete system snmp'\n",
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {
            "pe1": _diff(
                only_in_live={
                    "configure system snmp community A",
                    "configure system snmp community B",
                }
            )
        }

        out = project_remediation(results=results, session=session, remediation_dir=tmp_path)

        assert out[0].remediation_commands == ("delete system snmp",)

    def test_unknown_placeholder_raises_at_runtime(self, session, tmp_path):
        # The web validator blocks this at authoring time; a spec that reaches the engine
        # with a placeholder no group can fill must fail loud, not push a literal {subnet}.
        _write_spec(
            tmp_path,
            "pe",
            "delete:\n"
            "  - match: startswith\n"
            '    value: "configure x"\n'
            "    remediation_commands:\n"
            "      - 'delete {subnet}'\n",
        )
        _device(session, hostname="pe1", role_name="pe", status=DeviceStatus.active)
        results = {"pe1": _diff(only_in_live={"configure x 1"})}

        with pytest.raises(ValueError, match="subnet"):
            project_remediation(results=results, session=session, remediation_dir=tmp_path)
