from __future__ import annotations

"""Tests for the ``HalfOpenRings`` → ``add_cable`` derivation.

The sheet never names the ring's end members; the cabler derives them through
``app.domain.half_open_ring.terminating_devices``. These tests run the cabler
against ``tests/test.xlsx`` (three single-site rings on ``tst-001``) and check
the emitted end cables land on ``core1``/``core2`` with no port names, leaving
port selection to the topology builder.
"""

from app.excel_data_handling.excel_data_handler import ExcelDataHandler
from tests.conftest import WB_NAME


def _ring_cables(session) -> list[tuple[str, str]]:
    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)
    edh.create_actions_blob_for_pe_ring_cables_from_half_open_rings()
    return [
        (step["params"]["device_a_name"], step["params"]["device_b_name"])
        for step in edh.actions_blob
        if step["action"] == "add_cable"
    ]


def test_ring_end_cables_terminate_on_core_routers(session):
    cables = _ring_cables(session)

    # Paired PEs: core1 → pe1, pe2 → core2 (the pair is cabled internally).
    assert ("core1.tst-001", "pe1.tst-001") in cables
    assert ("pe2.tst-001", "core2.tst-001") in cables
    # Single PE rings: core1 → pe → core2.
    assert ("core1.tst-001", "pe3.tst-001") in cables
    assert ("pe3.tst-001", "core2.tst-001") in cables


def test_ring_cables_carry_no_port_names(session):
    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)
    edh.create_actions_blob_for_pe_ring_cables_from_half_open_rings()
    assert edh.actions_blob
    for step in edh.actions_blob:
        assert step["params"]["iface_a_name"] is None
        assert step["params"]["iface_b_name"] is None


def test_cables_sheet_row_without_ports_emits_none_ports(session, tmp_path):
    """A ``Cables`` row with empty ``Iface_a``/``Iface_b`` cells reaches the
    executor with ``None`` port names, so the topology builder auto-assigns
    free NNIs on both ends — the demo workbook relies on this."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Cables"
    ws.append(["Device_a", "Iface_a", "Device_b", "Iface_b"])
    ws.append(["core1.tst-001", None, "pe3.tst-001", None])
    ws.append(["core1.tst-001", "1/1/c1/1", "rr1.tst-001", "1/1/c2/1"])
    path = tmp_path / "cables.xlsx"
    wb.save(path)

    edh = ExcelDataHandler(session=session, wb_name=path)
    edh.create_actions_blob_for_cables_loaded_from_excel()

    assert [s["params"] for s in edh.actions_blob] == [
        {
            "device_a_name": "core1.tst-001",
            "device_b_name": "pe3.tst-001",
            "iface_a_name": None,
            "iface_b_name": None,
        },
        {
            "device_a_name": "core1.tst-001",
            "device_b_name": "rr1.tst-001",
            "iface_a_name": "1/1/c1/1",
            "iface_b_name": "1/1/c2/1",
        },
    ]
