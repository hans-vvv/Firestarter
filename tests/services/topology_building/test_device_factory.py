from __future__ import annotations

import pytest

from app.services.topology_building.device_factory import DeviceFactory


class TestExpandRange:
    """
    Unit tests for DeviceFactory._expand_range.

    Pure static method — no database or fixtures required. Covers both
    the happy-path expansion logic and every error branch.
    """

    def test_single_interface_returns_one_entry(self):
        result = DeviceFactory._expand_range("1/1/c1/1", "1/1/c1/1", "", "NNI", "c1-400g")
        assert len(result) == 1
        assert result[0]["name"] == "1/1/c1/1"
        assert result[0]["role"] == "NNI"
        assert result[0]["conn_name"] == "c1-400g"

    def test_range_expands_correct_count(self):
        result = DeviceFactory._expand_range("1/1/c1/1", "1/1/c5/1", "", "NNI", "c1-400g")
        assert len(result) == 5

    def test_range_produces_correct_names_in_order(self):
        result = DeviceFactory._expand_range("1/1/c1/1", "1/1/c3/1", "", "NNI", "c1-400g")
        names = [r["name"] for r in result]
        assert names == ["1/1/c1/1", "1/1/c2/1", "1/1/c3/1"]

    def test_base_prefix_prepended_to_each_name(self):
        result = DeviceFactory._expand_range("0/0/0", "0/0/2", "Ethernet", "UNI", None)
        names = [r["name"] for r in result]
        assert names == ["Ethernet0/0/0", "Ethernet0/0/1", "Ethernet0/0/2"]

    def test_all_entries_carry_role_and_conn_name(self):
        result = DeviceFactory._expand_range("1/1/c5/1", "1/1/c7/1", "", "UNI", "c1-25g")
        for entry in result:
            assert entry["role"] == "UNI"
            assert entry["conn_name"] == "c1-25g"

    def test_path_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="Path length mismatch"):
            DeviceFactory._expand_range("1/1/c1", "1/1/c1/1", "", "NNI", None)

    def test_multiple_varying_components_raises(self):
        with pytest.raises(ValueError, match="Exactly one path component may vary"):
            DeviceFactory._expand_range("1/1/c1/1", "1/2/c2/2", "", "NNI", None)

    def test_non_numeric_component_raises(self):
        with pytest.raises(ValueError, match="Non-numeric range component"):
            DeviceFactory._expand_range("1/1/ca/1", "1/1/cc/1", "", "NNI", None)

    def test_mismatched_prefixes_raises(self):
        with pytest.raises(ValueError, match="Mismatched range prefixes"):
            DeviceFactory._expand_range("1/1/c1/1", "1/1/x5/1", "", "NNI", None)
