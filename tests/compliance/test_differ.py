from __future__ import annotations

import pytest

from app.compliance.differ import ComplianceDiffer, DiffResult
from app.compliance.normaliser import NormalisedConfig


def _norm(hostname: str, lines: set[str], ignored: int = 0) -> NormalisedConfig:
    return NormalisedConfig(hostname=hostname, lines=frozenset(lines), ignored_count=ignored)


class TestDiffResult:
    def test_compliant_when_both_sides_empty(self):
        result = DiffResult(
            hostname="r1",
            only_in_rendered=frozenset(),
            only_in_live=frozenset(),
            ignored_rendered=0,
            ignored_live=0,
        )
        assert result.compliant is True
        assert result.drift_count == 0

    def test_compliant_when_no_diff(self):
        result = DiffResult(
            hostname="r1",
            only_in_rendered=frozenset(),
            only_in_live=frozenset(),
            ignored_rendered=10,
            ignored_live=8,
        )
        assert result.compliant is True

    def test_non_compliant_when_missing_lines(self):
        result = DiffResult(
            hostname="r1",
            only_in_rendered=frozenset({"configure router bgp"}),
            only_in_live=frozenset(),
            ignored_rendered=0,
            ignored_live=0,
        )
        assert result.compliant is False
        assert result.drift_count == 1

    def test_non_compliant_when_unexpected_lines(self):
        result = DiffResult(
            hostname="r1",
            only_in_rendered=frozenset(),
            only_in_live=frozenset({"configure router ospf stale-config"}),
            ignored_rendered=0,
            ignored_live=0,
        )
        assert result.compliant is False
        assert result.drift_count == 1

    def test_drift_count_sums_both_directions(self):
        result = DiffResult(
            hostname="r1",
            only_in_rendered=frozenset({"a", "b"}),
            only_in_live=frozenset({"c"}),
            ignored_rendered=0,
            ignored_live=0,
        )
        assert result.drift_count == 3


class TestComplianceDiffer:
    def setup_method(self):
        self.differ = ComplianceDiffer()

    def test_identical_configs_are_compliant(self):
        lines = {"configure router bgp", "configure system name r1"}
        result = self.differ.diff(
            rendered=_norm("r1", lines),
            live=_norm("r1", lines),
        )
        assert result.compliant is True
        assert result.only_in_rendered == frozenset()
        assert result.only_in_live == frozenset()

    def test_empty_configs_are_compliant(self):
        result = self.differ.diff(
            rendered=_norm("r1", set()),
            live=_norm("r1", set()),
        )
        assert result.compliant is True

    def test_detects_missing_lines(self):
        result = self.differ.diff(
            rendered=_norm("r1", {"a", "b", "c"}),
            live=_norm("r1", {"a"}),
        )
        assert result.only_in_rendered == frozenset({"b", "c"})
        assert result.only_in_live == frozenset()
        assert result.compliant is False

    def test_detects_unexpected_lines(self):
        result = self.differ.diff(
            rendered=_norm("r1", {"a"}),
            live=_norm("r1", {"a", "stale-line"}),
        )
        assert result.only_in_rendered == frozenset()
        assert result.only_in_live == frozenset({"stale-line"})
        assert result.compliant is False

    def test_detects_drift_in_both_directions(self):
        result = self.differ.diff(
            rendered=_norm("r1", {"a", "b"}),
            live=_norm("r1", {"b", "c"}),
        )
        assert result.only_in_rendered == frozenset({"a"})
        assert result.only_in_live == frozenset({"c"})

    def test_hostname_mismatch_raises_value_error(self):
        with pytest.raises(ValueError, match="Hostname mismatch"):
            self.differ.diff(
                rendered=_norm("r1", {"a"}),
                live=_norm("r2", {"a"}),
            )

    def test_carries_ignored_counts_from_both_sides(self):
        result = self.differ.diff(
            rendered=_norm("r1", {"a"}, ignored=5),
            live=_norm("r1", {"a"}, ignored=3),
        )
        assert result.ignored_rendered == 5
        assert result.ignored_live == 3

    def test_result_hostname_matches_input(self):
        result = self.differ.diff(
            rendered=_norm("core1.tst-001", {"x"}),
            live=_norm("core1.tst-001", {"x"}),
        )
        assert result.hostname == "core1.tst-001"

    def test_diff_many_processes_common_hostnames_only(self):
        rendered = {"r1": _norm("r1", {"a"}), "r2": _norm("r2", {"b"})}
        live = {"r1": _norm("r1", {"a"}), "r3": _norm("r3", {"c"})}
        results = self.differ.diff_many(rendered=rendered, live=live)
        assert set(results.keys()) == {"r1"}

    def test_diff_many_returns_keys_in_sorted_order(self):
        hostnames = ["zz", "aa", "mm"]
        rendered = {h: _norm(h, {"x"}) for h in hostnames}
        live = {h: _norm(h, {"x"}) for h in hostnames}
        results = self.differ.diff_many(rendered=rendered, live=live)
        assert list(results.keys()) == ["aa", "mm", "zz"]

    def test_diff_many_empty_inputs_returns_empty(self):
        assert self.differ.diff_many(rendered={}, live={}) == {}

    def test_diff_many_no_common_hostnames_returns_empty(self):
        rendered = {"r1": _norm("r1", {"a"})}
        live = {"r2": _norm("r2", {"b"})}
        assert self.differ.diff_many(rendered=rendered, live=live) == {}
