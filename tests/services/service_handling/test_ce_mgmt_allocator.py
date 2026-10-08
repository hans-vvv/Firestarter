from __future__ import annotations

import pytest

from app.models import (
    CeMgmtAddress,
    DelegatedPrefix,
    Device,
    Interface,
)
from app.models.orm_models import Role, Site
from app.services.service_handling import ce_mgmt_allocator as alloc
from app.services.service_handling.ce_mgmt_allocator import (
    _next_free_host,
    allocate_ce_mgmt_addresses,
    validate_ce_index_contiguity,
)

# Pair site (two pes share one /28) and a standalone pe in its own
# site (so per-(site, stem) contiguity groups never entangle across the two).
_PAIR_LABEL = "pe-pair:amt-001-1"
_PAIR_PREFIX = "10.10.0.0/28"
_STANDALONE_HOST = "pe1.brg-001"
_STANDALONE_PREFIX = "10.10.0.16/28"


@pytest.fixture
def scene(session):
    """A pe pair + a standalone pe, each with a CE-management /28.

    Exposes ``add_ce`` so individual tests can wire additional CEs (and their
    ``CE:`` access LAGs) to model attaching a new device.
    """
    amt = Site(name="amt-001")
    brg = Site(name="brg-001")
    pe_role = Role(name="pe")
    switch_role = Role(name="switch")
    test_switch_role = Role(name="test-switch")
    session.add_all([amt, brg, pe_role, switch_role, test_switch_role])
    session.flush()

    roles = {"switch": switch_role, "test-switch": test_switch_role}

    def _pe(hostname, *, site, pair_label):
        labels = {"tenant": "production"}
        if pair_label is not None:
            labels["pair_label"] = pair_label
        dev = Device(hostname=hostname, role_id=pe_role.id, site_id=site.id, labels=labels)
        session.add(dev)
        session.flush()
        return dev

    pe1 = _pe("pe1.amt-001", site=amt, pair_label=_PAIR_LABEL)
    pe2 = _pe("pe2.amt-001", site=amt, pair_label=_PAIR_LABEL)
    pe3 = _pe(_STANDALONE_HOST, site=brg, pair_label=None)  # falls back to hostname

    pes = {"pair": [pe1, pe2], "standalone": [pe3]}

    def add_ce(hostname, *, role, where, model="MF2"):
        site = amt if where == "pair" else brg
        dev = Device(
            hostname=hostname,
            role_id=roles[role].id,
            site_id=site.id,
            model_name=model,
            labels={},
        )
        session.add(dev)
        session.flush()
        for pe in pes[where]:
            session.add(
                Interface(
                    name="lag-1",
                    device_id=pe.id,
                    parent_id=None,
                    description=f"CE:{hostname} (access LAG id 1)",
                )
            )
        session.flush()

    session.add_all(
        [
            DelegatedPrefix(
                name=f"ce_mgmt_{_PAIR_LABEL}",
                in_use=True,
                reservations={"prefix": _PAIR_PREFIX},
            ),
            DelegatedPrefix(
                name=f"ce_mgmt_{_STANDALONE_HOST}",
                in_use=True,
                reservations={"prefix": _STANDALONE_PREFIX},
            ),
        ]
    )
    session.flush()

    # Base inventory: switch + two switches behind the pair, one switch behind standalone.
    add_ce("test-switch1.amt-001", role="test-switch", where="pair")
    add_ce("switch1.amt-001", role="switch", where="pair")
    add_ce("switch2.amt-001", role="switch", where="pair")
    add_ce("switch1.brg-001", role="switch", where="standalone")

    return alloc, add_ce


_BASE_ORDER = [
    "test-switch1.amt-001",
    "switch1.amt-001",
    "switch2.amt-001",
    "switch1.brg-001",
]


def _set_excel_order(monkeypatch, order):
    monkeypatch.setattr(alloc, "_load_excel_ce_order", lambda wb_name: list(order))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
class TestValidateContiguity:
    def test_contiguous_indices_pass(self):
        validate_ce_index_contiguity(
            ["switch1.s", "switch2.s", "switch3.s", "test-switch1.s", "test-switch2.s"]
        )

    def test_single_index_per_site_passes(self):
        validate_ce_index_contiguity(["switch1.a", "switch1.b", "switch1.c"])

    def test_gap_raises_and_names_missing(self):
        with pytest.raises(ValueError, match=r"switch2\.s"):
            validate_ce_index_contiguity(["switch1.s", "switch3.s"])

    def test_missing_first_index_is_a_gap(self):
        with pytest.raises(ValueError, match=r"switch1\.s"):
            validate_ce_index_contiguity(["switch2.s", "switch3.s"])

    def test_gap_isolated_per_site_and_stem(self):
        with pytest.raises(ValueError, match=r"switch2\.s"):
            validate_ce_index_contiguity(["switch1.s", "switch3.s", "test-switch1.s", "switch1.t"])

    def test_node_without_index_is_ignored(self):
        validate_ce_index_contiguity(["gw.s", "switch1.s"])


class TestNextFreeHost:
    def test_starts_at_fourth_usable(self):
        assert _next_free_host("10.0.0.0/28", set()) == "10.0.0.4"

    def test_skips_used_hosts(self):
        assert _next_free_host("10.0.0.0/28", {"10.0.0.4", "10.0.0.5"}) == "10.0.0.6"

    def test_respects_subnet_offset(self):
        assert _next_free_host("10.10.0.16/28", set()) == "10.10.0.20"

    def test_exhaustion_raises(self):
        used = {f"10.0.0.{n}" for n in range(4, 15)}  # fill .4-.14
        with pytest.raises(ValueError, match="exhausted"):
            _next_free_host("10.0.0.0/28", used)


# ---------------------------------------------------------------------------
# Allocation (integration against the synthetic scene)
# ---------------------------------------------------------------------------
class TestAllocate:
    def test_assigns_in_excel_order_from_fourth_usable(self, session, scene, monkeypatch):
        _set_excel_order(monkeypatch, _BASE_ORDER)
        by_host = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}
        # Pair /28: switch first (Excel order), then the two switches — all from .4.
        assert by_host["test-switch1.amt-001"] == "10.10.0.4"
        assert by_host["switch1.amt-001"] == "10.10.0.5"
        assert by_host["switch2.amt-001"] == "10.10.0.6"
        # Standalone pe has its own /28 → its switch starts fresh at .20.
        assert by_host["switch1.brg-001"] == "10.10.0.20"

    def test_is_sticky_across_reruns(self, session, scene, monkeypatch):
        _set_excel_order(monkeypatch, _BASE_ORDER)
        first = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}
        second = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}
        assert first == second
        assert session.query(CeMgmtAddress).count() == 4

    def test_new_ce_takes_next_free_without_moving_others(self, session, scene, monkeypatch):
        _, add_ce = scene
        _set_excel_order(monkeypatch, _BASE_ORDER)
        first = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}

        # Onboard switch3 behind the pair (highest index → contiguity-safe).
        add_ce("switch3.amt-001", role="switch", where="pair")
        _set_excel_order(monkeypatch, [*_BASE_ORDER, "switch3.amt-001"])
        second = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}

        for host in _BASE_ORDER:
            assert second[host] == first[host]  # sticky
        assert second["switch3.amt-001"] == "10.10.0.7"  # next free in the pair /28

    def test_removed_ce_is_pruned_and_slot_freed(self, session, scene, monkeypatch):
        _set_excel_order(monkeypatch, _BASE_ORDER)
        allocate_ce_mgmt_addresses(session=session)  # switch2 → .6

        # Decommission switch2 (drop its access LAGs on the pes and its device
        # row) and remove it from the sheet — a decommissioned CE.
        for lag in session.query(Interface).filter(
            Interface.description.like("CE:switch2.amt-001%")
        ):
            session.delete(lag)
        session.query(Device).filter(Device.hostname == "switch2.amt-001").delete()
        session.flush()
        _set_excel_order(
            monkeypatch, ["test-switch1.amt-001", "switch1.amt-001", "switch1.brg-001"]
        )
        rows = allocate_ce_mgmt_addresses(session=session)
        by_host = {r.ce_hostname: r.address for r in rows}

        assert "switch2.amt-001" not in by_host  # pruned
        assert by_host["switch1.amt-001"] == "10.10.0.5"  # unchanged
        assert session.query(CeMgmtAddress).count() == 3

        # The freed .6 is reclaimed by the next onboarded CE.
        _, add_ce = scene
        add_ce("switch2.amt-001", role="switch", where="pair")  # re-add (back to switch1,switch2)
        _set_excel_order(monkeypatch, _BASE_ORDER)
        again = {r.ce_hostname: r.address for r in allocate_ce_mgmt_addresses(session=session)}
        assert again["switch2.amt-001"] == "10.10.0.6"

    def test_missing_delegated_prefix_raises(self, session, scene, monkeypatch):
        session.query(DelegatedPrefix).filter(
            DelegatedPrefix.name == f"ce_mgmt_{_STANDALONE_HOST}"
        ).delete()
        session.flush()
        _set_excel_order(monkeypatch, _BASE_ORDER)
        with pytest.raises(ValueError, match="No CE-management delegated /28"):
            allocate_ce_mgmt_addresses(session=session)

    def test_contiguity_gap_raises_during_allocation(self, session, scene, monkeypatch):
        _set_excel_order(monkeypatch, ["switch1.amt-001", "switch3.amt-001"])
        with pytest.raises(ValueError, match=r"switch2\.amt-001"):
            allocate_ce_mgmt_addresses(session=session)
