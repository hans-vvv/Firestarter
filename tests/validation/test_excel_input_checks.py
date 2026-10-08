from __future__ import annotations

import pandas as pd
import pytest

from app.validation.excel_input_checks import (
    ValidationError,
    _excel_row,
    _norm,
    _parse_device_cell,
    collect_used_sites,
    validate_cables,
    validate_devices,
    validate_dist_devices,
    validate_excel_inputs,
    validate_half_open_rings,
    validate_mutual_exclusive,
    validate_site_sheet,
    validate_sites,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _df_devices(*rows) -> pd.DataFrame:
    """Build a Devices-sheet DataFrame from (DeviceName, Tenant) tuples."""
    return pd.DataFrame(rows, columns=["DeviceName", "Tenant"])


def _df_dist(*rows) -> pd.DataFrame:
    """Build a DistDevices-sheet DataFrame."""
    return pd.DataFrame(rows, columns=["DeviceName", "RoleName", "SiteName", "ModelName", "Tenant"])


def _df_cables(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Device_a", "Iface_a", "Device_b", "Iface_b"])


def _df_sites(*names) -> pd.DataFrame:
    return pd.DataFrame({"SiteName": list(names)})


# ---------------------------------------------------------------------------
# _excel_row
# ---------------------------------------------------------------------------


class TestExcelRow:
    def test_zero_index_maps_to_row_2(self):
        assert _excel_row(0) == 2

    def test_index_5_maps_to_row_7(self):
        assert _excel_row(5) == 7


# ---------------------------------------------------------------------------
# _norm
# ---------------------------------------------------------------------------


class TestNorm:
    def test_none_returns_empty(self):
        assert _norm(None) == ""

    def test_strips_whitespace(self):
        assert _norm("  hello  ") == "hello"

    def test_converts_non_string(self):
        assert _norm(42) == "42"

    def test_empty_string_stays_empty(self):
        assert _norm("") == ""


# ---------------------------------------------------------------------------
# _parse_device_cell
# ---------------------------------------------------------------------------


class TestParseDeviceCell:
    def test_single_device(self):
        assert _parse_device_cell("devA") == ["devA"]

    def test_two_devices(self):
        result = _parse_device_cell("devA,devB")
        assert set(result) == {"devA", "devB"}
        assert len(result) == 2

    def test_blank_returns_empty(self):
        assert _parse_device_cell("") == []

    def test_none_returns_empty(self):
        assert _parse_device_cell(None) == []

    def test_strips_spaces_around_names(self):
        result = _parse_device_cell(" devA , devB ")
        assert "devA" in result
        assert "devB" in result

    def test_ignores_empty_parts(self):
        result = _parse_device_cell("devA,,devB")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# validate_devices
# ---------------------------------------------------------------------------


class TestValidateDevices:
    def test_valid_rows_no_errors(self):
        df = _df_devices(("host1", "lab"), ("host2", "lab"))
        errors, devices = validate_devices(df)
        assert errors == []
        assert devices == {"host1", "host2"}

    def test_missing_column_returns_single_error(self):
        df = pd.DataFrame({"Other": ["x"]})
        errors, _devices = validate_devices(df)
        assert len(errors) == 1
        assert "Missing column" in errors[0].message

    def test_empty_device_name_is_error(self):
        df = _df_devices(("", "lab"))
        errors, _ = validate_devices(df)
        assert any("Empty DeviceName" in e.message for e in errors)

    def test_empty_tenant_is_error(self):
        df = _df_devices(("host1", ""))
        errors, _ = validate_devices(df)
        assert any("Tenant" in e.message for e in errors)

    def test_duplicate_device_name_is_error(self):
        df = _df_devices(("host1", "lab"), ("host1", "lab"))
        errors, _ = validate_devices(df)
        assert any("Duplicate" in e.message for e in errors)

    def test_empty_device_not_added_to_device_set(self):
        df = _df_devices(("", "lab"), ("host1", "lab"))
        _, devices = validate_devices(df)
        assert "" not in devices
        assert "host1" in devices


# ---------------------------------------------------------------------------
# validate_dist_devices
# ---------------------------------------------------------------------------


class TestValidateDistDevices:
    def test_valid_single_device(self):
        df = _df_dist(("devA", "pe", "site1", "model1", "lab"))
        errors, seen, pairs = validate_dist_devices(df)
        assert errors == []
        assert "devA" in seen
        assert pairs == set()

    def test_valid_pair_in_one_cell(self):
        df = _df_dist(("devA,devB", "pe", "site1", "model1", "lab"))
        errors, seen, pairs = validate_dist_devices(df)
        assert errors == []
        assert {"devA", "devB"} <= seen
        assert len(pairs) == 1

    def test_missing_required_columns_returns_errors(self):
        df = pd.DataFrame({"DeviceName": ["devA"]})
        errors, _, _ = validate_dist_devices(df)
        assert any("Missing column" in e.message for e in errors)

    def test_empty_device_name_is_error(self):
        df = _df_dist(("", "pe", "site1", "model1", "lab"))
        errors, _, _ = validate_dist_devices(df)
        assert any("Empty DeviceName" in e.message for e in errors)

    def test_more_than_two_devices_is_error(self):
        df = _df_dist(("a,b,c", "pe", "site1", "model1", "lab"))
        errors, _, _ = validate_dist_devices(df)
        assert any("Invalid DeviceName" in e.message for e in errors)

    def test_empty_tenant_is_error(self):
        df = _df_dist(("devA", "pe", "site1", "model1", ""))
        errors, _, _ = validate_dist_devices(df)
        assert any("Tenant" in e.message for e in errors)

    def test_duplicate_device_is_error(self):
        df = _df_dist(
            ("devA", "pe", "site1", "model1", "lab"),
            ("devA", "pe", "site2", "model1", "lab"),
        )
        errors, _, _ = validate_dist_devices(df)
        assert any("Duplicate" in e.message for e in errors)

    def test_pair_normalized_sorted(self):
        df = _df_dist(("devB,devA", "pe", "site1", "model1", "lab"))
        _, _, pairs = validate_dist_devices(df)
        assert ("devA", "devB") in pairs


# ---------------------------------------------------------------------------
# validate_mutual_exclusive
# ---------------------------------------------------------------------------


class TestValidateMutualExclusive:
    def test_no_overlap_no_errors(self):
        assert validate_mutual_exclusive({"a", "b"}, {"c", "d"}) == []

    def test_overlap_produces_error_per_device(self):
        errors = validate_mutual_exclusive({"a", "b"}, {"b", "c"})
        assert len(errors) == 1
        assert "b" in errors[0].message

    def test_multiple_overlaps(self):
        errors = validate_mutual_exclusive({"a", "b"}, {"a", "b"})
        assert len(errors) == 2

    def test_empty_sets_no_errors(self):
        assert validate_mutual_exclusive(set(), set()) == []


# ---------------------------------------------------------------------------
# validate_cables
# ---------------------------------------------------------------------------


class TestValidateCables:
    def test_valid_cable_no_errors(self):
        df = _df_cables(("devA", "eth0", "devB", "eth0"))
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert errors == []

    def test_missing_column_returns_error(self):
        df = pd.DataFrame({"Device_a": ["devA"]})
        errors = validate_cables(df, all_devices={"devA"})
        assert any("Missing column" in e.message for e in errors)

    def test_empty_device_a_is_error(self):
        df = _df_cables(("", "eth0", "devB", "eth0"))
        errors = validate_cables(df, all_devices={"devB"})
        assert any("Empty Device_a" in e.message for e in errors)

    def test_empty_device_b_is_error(self):
        df = _df_cables(("devA", "eth0", "", "eth0"))
        errors = validate_cables(df, all_devices={"devA"})
        assert any("Empty Device_b" in e.message for e in errors)

    def test_unknown_device_a_is_error(self):
        df = _df_cables(("ghost", "eth0", "devB", "eth0"))
        errors = validate_cables(df, all_devices={"devB"})
        assert any("Unknown device" in e.message and "ghost" in e.message for e in errors)

    def test_unknown_device_b_is_error(self):
        df = _df_cables(("devA", "eth0", "ghost", "eth0"))
        errors = validate_cables(df, all_devices={"devA"})
        assert any("Unknown device" in e.message and "ghost" in e.message for e in errors)

    def test_self_link_same_device_and_interface_is_error(self):
        df = _df_cables(("devA", "eth0", "devA", "eth0"))
        errors = validate_cables(df, all_devices={"devA"})
        assert any("endpoint A equals endpoint B" in e.message for e in errors)

    def test_self_link_same_device_different_interface_is_allowed(self):
        df = _df_cables(("devA", "eth0", "devA", "eth1"))
        errors = validate_cables(df, all_devices={"devA"})
        assert not any("endpoint A equals endpoint B" in e.message for e in errors)

    def test_duplicate_cable_is_error(self):
        df = _df_cables(
            ("devA", "eth0", "devB", "eth1"),
            ("devA", "eth0", "devB", "eth1"),
        )
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert any("Duplicate cable" in e.message for e in errors)

    def test_duplicate_cable_reversed_order_is_error(self):
        df = _df_cables(
            ("devA", "eth0", "devB", "eth1"),
            ("devB", "eth1", "devA", "eth0"),
        )
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert any("Duplicate cable" in e.message for e in errors)

    def test_cable_without_ports_is_valid(self):
        """Empty Iface_a/Iface_b cells are allowed: the topology builder then
        picks a free NNI on each side (auto port assignment), exactly as it does
        for the ring cables the HalfOpenRings sheet derives."""
        df = _df_cables(("devA", None, "devB", None))
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert errors == []

    def test_cable_with_one_port_is_valid(self):
        """A port on only one side is fine too — the other end is auto-assigned."""
        df = _df_cables(("devA", "eth0", "devB", None))
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert errors == []

    def test_partial_cable_no_iface_not_flagged_as_duplicate(self):
        df = _df_cables(
            ("devA", "", "devB", ""),
            ("devA", "", "devB", ""),
        )
        errors = validate_cables(df, all_devices={"devA", "devB"})
        assert not any("Duplicate" in e.message for e in errors)


# ---------------------------------------------------------------------------
# validate_half_open_rings
# ---------------------------------------------------------------------------


class TestValidateHalfOpenRings:
    def _df(self, *rows) -> pd.DataFrame:
        cols = ["Termination_site_a", "Termination_site_b", "site1"]
        return pd.DataFrame(rows, columns=cols)

    def test_missing_termination_column_is_error(self):
        df = pd.DataFrame({"Termination_site_a": ["x"]})
        errors = validate_half_open_rings(df, dist_devices=set(), pe_pairs=set())
        assert any("Missing column" in e.message for e in errors)

    def test_empty_termination_site_is_error(self):
        df = self._df(("", "siteB", None))
        errors = validate_half_open_rings(df, dist_devices=set(), pe_pairs=set())
        assert any("Empty Termination_site_a" in e.message for e in errors)

    def test_device_not_in_dist_is_error(self):
        df = self._df(("siteA", "siteB", "devX"))
        errors = validate_half_open_rings(df, dist_devices=set(), pe_pairs=set())
        assert any("not present in DistDevices" in e.message for e in errors)

    def test_valid_single_device_no_errors(self):
        df = self._df(("siteA", "siteB", "devA"))
        errors = validate_half_open_rings(df, dist_devices={"devA"}, pe_pairs=set())
        assert errors == []

    def test_pair_not_in_pe_pairs_is_error(self):
        df = self._df(("siteA", "siteB", "devA,devB"))
        errors = validate_half_open_rings(df, dist_devices={"devA", "devB"}, pe_pairs=set())
        assert any("not paired in DistDevices" in e.message for e in errors)

    def test_valid_pair_in_pe_pairs_no_errors(self):
        df = self._df(("siteA", "siteB", "devA,devB"))
        errors = validate_half_open_rings(
            df,
            dist_devices={"devA", "devB"},
            pe_pairs={("devA", "devB")},
        )
        assert errors == []

    def test_device_in_two_rings_is_error(self):
        df = pd.DataFrame(
            [
                ("siteA", "siteB", "devA"),
                ("siteC", "siteD", "devA"),
            ],
            columns=["Termination_site_a", "Termination_site_b", "site1"],
        )
        errors = validate_half_open_rings(df, dist_devices={"devA"}, pe_pairs=set())
        assert any("two rings" in e.message for e in errors)

    def test_pe_pair_in_dist_but_not_paired_in_ring_is_error(self):
        # devA and devB are declared as a PE pair in DistDevices,
        # but in the ring each appears in its own cell (not together)
        df = pd.DataFrame(
            [("siteA", "siteB", "devA", "devB")],
            columns=["Termination_site_a", "Termination_site_b", "site1", "site2"],
        )
        errors = validate_half_open_rings(
            df,
            dist_devices={"devA", "devB"},
            pe_pairs={("devA", "devB")},
        )
        assert any("not paired in HalfOpenRings" in e.message for e in errors)


# ---------------------------------------------------------------------------
# collect_used_sites
# ---------------------------------------------------------------------------


class TestCollectUsedSites:
    def test_collects_from_devices(self):
        df_dev = pd.DataFrame({"Site": ["site1", "site2"]})
        result = collect_used_sites(df_dev, None)
        assert result == {"site1", "site2"}

    def test_collects_from_dist(self):
        df_dev = pd.DataFrame({"Site": ["site1"]})
        df_cp = pd.DataFrame({"SiteName": ["site2", "site3"]})
        result = collect_used_sites(df_dev, df_cp)
        assert "site2" in result
        assert "site3" in result

    def test_ignores_missing_columns(self):
        df_dev = pd.DataFrame({"Other": ["x"]})
        result = collect_used_sites(df_dev, None)
        assert result == set()

    def test_ignores_blank_and_nan(self):
        df_dev = pd.DataFrame({"Site": ["site1", None, "  "]})
        result = collect_used_sites(df_dev, None)
        assert result == {"site1"}


# ---------------------------------------------------------------------------
# validate_site_sheet
# ---------------------------------------------------------------------------


class TestValidateSiteSheet:
    def test_unique_sites_no_errors(self):
        df = _df_sites("site1", "site2", "site3")
        assert validate_site_sheet(df) == []

    def test_duplicate_site_is_error(self):
        df = _df_sites("site1", "site2", "site1")
        errors = validate_site_sheet(df)
        assert len(errors) == 1
        assert "Duplicate SiteName" in errors[0].message
        assert "site1" in errors[0].message

    def test_duplicate_points_to_first_occurrence_row(self):
        # rows: site1 (Excel row 2), site2 (row 3), site1 again (row 4)
        df = _df_sites("site1", "site2", "site1")
        errors = validate_site_sheet(df)
        assert errors[0].row == 2

    def test_duplicate_after_whitespace_strip_is_error(self):
        df = _df_sites("site1", "  site1  ")
        errors = validate_site_sheet(df)
        assert any("site1" in e.message for e in errors)

    def test_one_error_per_duplicated_name(self):
        df = _df_sites("a", "a", "a", "b", "b")
        errors = validate_site_sheet(df)
        names_flagged = sorted(e.message for e in errors)
        assert len(errors) == 2
        assert any("'a'" in m for m in names_flagged)
        assert any("'b'" in m for m in names_flagged)

    def test_missing_column_is_error(self):
        df = pd.DataFrame({"Other": ["x"]})
        errors = validate_site_sheet(df)
        assert len(errors) == 1
        assert "Missing column" in errors[0].message

    def test_blank_and_nan_rows_ignored(self):
        df = pd.DataFrame({"SiteName": ["site1", None, "  ", float("nan")]})
        assert validate_site_sheet(df) == []


# ---------------------------------------------------------------------------
# validate_sites
# ---------------------------------------------------------------------------


class TestValidateSites:
    def test_all_sites_known_no_errors(self):
        df = _df_sites("site1", "site2")
        errors = validate_sites(df, used_sites={"site1"})
        assert errors == []

    def test_missing_site_is_error(self):
        df = _df_sites("site1")
        errors = validate_sites(df, used_sites={"site1", "site2"})
        assert any("site2" in e.message for e in errors)

    def test_missing_column_is_error(self):
        df = pd.DataFrame({"Other": ["x"]})
        errors = validate_sites(df, used_sites={"site1"})
        assert any("Missing column" in e.message for e in errors)

    def test_empty_used_sites_no_errors(self):
        df = _df_sites("site1")
        assert validate_sites(df, used_sites=set()) == []


# ---------------------------------------------------------------------------
# validate_excel_inputs — orchestrator
# ---------------------------------------------------------------------------


class TestValidateExcelInputs:
    def _sites_df(self):
        return _df_sites("site1")

    def test_clean_input_no_errors(self):
        df_dev = pd.DataFrame(
            {
                "DeviceName": ["devA", "devB"],
                "Tenant": ["lab", "lab"],
                "Site": ["site1", "site1"],
            }
        )
        df_cables = _df_cables(("devA", "eth0", "devB", "eth0"))
        errors = validate_excel_inputs(
            df_devices=df_dev,
            df_dist=None,
            df_cables=df_cables,
            df_half_open_rings=None,
            df_sites=self._sites_df(),
        )
        assert errors == []

    def test_device_in_both_sheets_reported(self):
        df_dev = pd.DataFrame(
            {
                "DeviceName": ["devA"],
                "Tenant": ["lab"],
                "Site": ["site1"],
            }
        )
        df_cp = _df_dist(("devA", "pe", "site1", "model1", "lab"))
        df_cables = _df_cables()
        errors = validate_excel_inputs(
            df_devices=df_dev,
            df_dist=df_cp,
            df_cables=df_cables,
            df_half_open_rings=None,
            df_sites=self._sites_df(),
        )
        assert any("mutually exclusive" in e.message for e in errors)

    def test_unknown_device_in_cable_is_reported(self):
        df_dev = pd.DataFrame(
            {
                "DeviceName": ["devA"],
                "Tenant": ["lab"],
                "Site": ["site1"],
            }
        )
        df_cables = _df_cables(("devA", "eth0", "ghost", "eth0"))
        errors = validate_excel_inputs(
            df_devices=df_dev,
            df_dist=None,
            df_cables=df_cables,
            df_half_open_rings=None,
            df_sites=self._sites_df(),
        )
        assert any("Unknown device" in e.message for e in errors)

    def test_missing_site_is_reported(self):
        df_dev = pd.DataFrame(
            {
                "DeviceName": ["devA"],
                "Tenant": ["lab"],
                "Site": ["missing_site"],
            }
        )
        df_cables = _df_cables()
        errors = validate_excel_inputs(
            df_devices=df_dev,
            df_dist=None,
            df_cables=df_cables,
            df_half_open_rings=None,
            df_sites=_df_sites("site1"),
        )
        assert any("missing_site" in e.message for e in errors)

    def test_duplicate_site_name_is_reported(self):
        df_dev = pd.DataFrame(
            {
                "DeviceName": ["devA", "devB"],
                "Tenant": ["lab", "lab"],
                "Site": ["site1", "site1"],
            }
        )
        df_cables = _df_cables(("devA", "eth0", "devB", "eth0"))
        errors = validate_excel_inputs(
            df_devices=df_dev,
            df_dist=None,
            df_cables=df_cables,
            df_half_open_rings=None,
            df_sites=_df_sites("site1", "site1"),
        )
        assert any("Duplicate SiteName" in e.message for e in errors)
