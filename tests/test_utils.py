from __future__ import annotations

import pytest

from app.utils import require
from app.utils.utils import (
    Tree,
    cidr_to_address_mask,
    deep_merge,
    peer_ip_on_p2p,
)

# ---------------------------------------------------------------------------
# require()
# ---------------------------------------------------------------------------


class TestRequire:
    def test_returns_value_when_present(self):
        assert require("hello", "msg") == "hello"

    def test_raises_on_none(self):
        with pytest.raises(ValueError, match="my message"):
            require(None, "my message")

    def test_raises_on_empty_string(self):
        with pytest.raises(ValueError, match="msg"):
            require("", "msg")

    def test_raises_on_whitespace_string(self):
        with pytest.raises(ValueError, match="msg"):
            require("   ", "msg")

    def test_raises_on_empty_list(self):
        with pytest.raises(ValueError, match="msg"):
            require([], "msg")

    def test_raises_on_empty_dict(self):
        with pytest.raises(ValueError, match="msg"):
            require({}, "msg")

    def test_raises_on_empty_set(self):
        with pytest.raises(ValueError, match="msg"):
            require(set(), "msg")

    def test_zero_is_allowed(self):
        assert require(0, "msg") == 0

    def test_rejects_bool_false(self):
        """Bool input is a misuse — require() means presence, not truthiness."""
        with pytest.raises(TypeError, match="does not accept bool"):
            require(False, "msg")

    def test_rejects_bool_true(self):
        """Even True is rejected — bool is the wrong category entirely."""
        with pytest.raises(TypeError, match="does not accept bool"):
            require(True, "msg")

    def test_non_empty_list_is_allowed(self):
        assert require([1, 2], "msg") == [1, 2]

    def test_non_empty_dict_is_allowed(self):
        assert require({"k": "v"}, "msg") == {"k": "v"}

    def test_error_message_appears_in_exception(self):
        with pytest.raises(ValueError, match="specific error text"):
            require(None, "specific error text")

    def test_caller_context_in_error_message(self):
        """Error message includes the calling function name."""
        with pytest.raises(ValueError, match="missing") as exc_info:
            require(None, "missing")
        assert "test_caller_context_in_error_message" in str(exc_info.value)


# ---------------------------------------------------------------------------
# deep_merge()
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_non_overlapping_keys_both_present(self):
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_replaces_scalar(self):
        assert deep_merge({"a": 1}, {"a": 99})["a"] == 99

    def test_nested_dicts_merged_recursively(self):
        result = deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"b": 99}})
        assert result == {"a": {"b": 99, "c": 2}}

    def test_deeply_nested_merge(self):
        base = {"x": {"y": {"z": 1}}}
        override = {"x": {"y": {"w": 2}}}
        result = deep_merge(base, override)
        assert result == {"x": {"y": {"z": 1, "w": 2}}}

    def test_base_is_not_mutated(self):
        base = {"a": {"b": 1}}
        deep_merge(base, {"a": {"c": 2}})
        assert base == {"a": {"b": 1}}

    def test_override_scalar_replaces_nested_dict(self):
        result = deep_merge({"a": {"b": 1}}, {"a": "flat"})
        assert result["a"] == "flat"

    def test_override_dict_replaces_scalar(self):
        result = deep_merge({"a": "flat"}, {"a": {"b": 1}})
        assert result["a"] == {"b": 1}

    def test_empty_override_returns_copy_of_base(self):
        base = {"a": 1}
        result = deep_merge(base, {})
        assert result == base
        assert result is not base

    def test_empty_base_returns_copy_of_override(self):
        override = {"a": 1}
        result = deep_merge({}, override)
        assert result == override

    def test_list_values_are_replaced_not_merged(self):
        """Lists are treated as scalars — override wins entirely."""
        result = deep_merge({"a": [1, 2]}, {"a": [3]})
        assert result["a"] == [3]


# ---------------------------------------------------------------------------
# peer_ip_on_p2p()
# ---------------------------------------------------------------------------


class TestPeerIpOnP2p:
    def test_p31_first_address_returns_second(self):
        assert peer_ip_on_p2p("10.0.0.0/31") == "10.0.0.1"

    def test_p31_second_address_returns_first(self):
        assert peer_ip_on_p2p("10.0.0.1/31") == "10.0.0.0"

    def test_p31_non_zero_subnet(self):
        assert peer_ip_on_p2p("10.0.4.18/31") == "10.0.4.19"
        assert peer_ip_on_p2p("10.0.4.19/31") == "10.0.4.18"

    def test_p30_first_host_returns_second(self):
        assert peer_ip_on_p2p("10.0.0.1/30") == "10.0.0.2"

    def test_p30_second_host_returns_first(self):
        assert peer_ip_on_p2p("10.0.0.2/30") == "10.0.0.1"

    def test_p30_network_address_raises(self):
        with pytest.raises(ValueError, match="not a usable host address"):
            peer_ip_on_p2p("10.0.0.0/30")

    def test_p30_broadcast_address_raises(self):
        with pytest.raises(ValueError, match="not a usable host address"):
            peer_ip_on_p2p("10.0.0.3/30")

    def test_p29_raises(self):
        with pytest.raises(ValueError, match="not /30 or /31"):
            peer_ip_on_p2p("10.0.0.1/29")

    def test_p32_raises(self):
        with pytest.raises(ValueError, match="not /30 or /31"):
            peer_ip_on_p2p("10.0.0.1/32")

    def test_p24_raises(self):
        with pytest.raises(ValueError, match="not /30 or /31"):
            peer_ip_on_p2p("10.0.0.1/24")


# ---------------------------------------------------------------------------
# Tree (autovivification dict)
# ---------------------------------------------------------------------------


class TestTree:
    def test_autovivifies_nested_keys(self):
        t = Tree()
        t["a"]["b"]["c"] = 42
        assert t["a"]["b"]["c"] == 42

    def test_is_a_dict_subclass(self):
        assert isinstance(Tree(), dict)

    def test_existing_key_not_overwritten_on_access(self):
        t = Tree()
        t["a"] = "value"
        assert t["a"] == "value"

    def test_str_returns_valid_json(self):
        import json

        t = Tree()
        t["key"] = "val"
        parsed = json.loads(str(t))
        assert parsed["key"] == "val"

    def test_nested_autovivification_then_assignment(self):
        t = Tree()
        t["x"]["y"].setdefault("items", []).append(1)
        assert t["x"]["y"]["items"] == [1]


# ---------------------------------------------------------------------------
# cidr_to_address_mask()
# ---------------------------------------------------------------------------


class TestCidrToAddressMask:
    def test_slash24(self):
        addr, mask = cidr_to_address_mask("10.0.0.1/24")
        assert addr == "10.0.0.1"
        assert mask == "255.255.255.0"

    def test_slash16(self):
        addr, mask = cidr_to_address_mask("172.16.0.1/16")
        assert addr == "172.16.0.1"
        assert mask == "255.255.0.0"

    def test_slash31(self):
        addr, mask = cidr_to_address_mask("192.168.1.0/31")
        assert addr == "192.168.1.0"
        assert mask == "255.255.255.254"

    def test_slash32(self):
        addr, mask = cidr_to_address_mask("10.1.1.1/32")
        assert addr == "10.1.1.1"
        assert mask == "255.255.255.255"
