from __future__ import annotations

import pytest

from app.services.context.composers.bgp import compose_bgp
from app.services.context.composers.evpn_esi import compose_evpn_esi
from app.services.context.composers.evpn_vpls import compose_evpn_vpls
from app.services.context.composers.isis import compose_isis
from app.services.context.composers.sr import compose_sr
from app.services.context.composers.vprn import compose_vprn
from app.services.context.services_context import compose_services

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _device_ctx(*iface_names: str) -> dict:
    """Minimal device context with named interfaces."""
    return {
        "hostname": "r1.tst-001",
        "interfaces": [{"name": n, "description": ""} for n in iface_names],
    }


# ---------------------------------------------------------------------------
# compose_isis
# ---------------------------------------------------------------------------


class TestComposeIsis:
    def test_attaches_isis_block_to_matching_interface(self):
        ctx = _device_ctx("lag-1")
        compose_isis(
            device_ctx=ctx,
            isis_intent={"process_id": "1", "interfaces": [{"iface_name": "lag-1", "metric": 10}]},
        )
        assert ctx["interfaces"][0]["isis"]["metric"] == 10

    def test_device_level_result_excludes_interfaces_key(self):
        ctx = _device_ctx("lag-1")
        result = compose_isis(
            device_ctx=ctx,
            isis_intent={"process_id": "1", "interfaces": [{"iface_name": "lag-1"}]},
        )
        assert "interfaces" not in result
        assert result["process_id"] == "1"

    def test_raises_on_unknown_interface(self):
        ctx = _device_ctx("lag-1")
        with pytest.raises(RuntimeError, match="unknown interface"):
            compose_isis(
                device_ctx=ctx,
                isis_intent={"interfaces": [{"iface_name": "lag-99"}]},
            )

    def test_no_interfaces_in_intent_leaves_device_ctx_unchanged(self):
        ctx = _device_ctx("lag-1")
        result = compose_isis(
            device_ctx=ctx,
            isis_intent={"process_id": "1", "level_type": "level-2-only"},
        )
        assert result["level_type"] == "level-2-only"
        assert "isis" not in ctx["interfaces"][0]

    def test_multiple_interfaces_all_decorated(self):
        ctx = _device_ctx("lag-1", "lag-2")
        compose_isis(
            device_ctx=ctx,
            isis_intent={
                "interfaces": [
                    {"iface_name": "lag-1", "metric": 10},
                    {"iface_name": "lag-2", "metric": 20},
                ]
            },
        )
        metrics = {i["name"]: i["isis"]["metric"] for i in ctx["interfaces"]}
        assert metrics == {"lag-1": 10, "lag-2": 20}

    def test_iface_cfg_minus_iface_name_is_stored(self):
        """Only the keys other than iface_name go into the isis block."""
        ctx = _device_ctx("lag-1")
        compose_isis(
            device_ctx=ctx,
            isis_intent={"interfaces": [{"iface_name": "lag-1", "metric": 5, "passive": False}]},
        )
        iface_isis = ctx["interfaces"][0]["isis"]
        assert "iface_name" not in iface_isis
        assert iface_isis == {"metric": 5, "passive": False}


# ---------------------------------------------------------------------------
# compose_bgp
# ---------------------------------------------------------------------------


class TestComposeBgp:
    def test_merges_rapid_update_afs_across_variants(self):
        intent = {
            "variant": {
                "rr": {"rapid_update_afs": ["vpn-ipv4"]},
                "client": {"rapid_update_afs": ["evpn", "vpn-ipv4"]},
            }
        }
        result = compose_bgp(device_ctx={}, bgp_intent=intent)
        assert set(result["rapid_update_afs"]) == {"vpn-ipv4", "evpn"}

    def test_deduplicates_rapid_update_afs(self):
        intent = {
            "variant": {
                "a": {"rapid_update_afs": ["vpn-ipv4"]},
                "b": {"rapid_update_afs": ["vpn-ipv4"]},
            }
        }
        result = compose_bgp(device_ctx={}, bgp_intent=intent)
        assert result["rapid_update_afs"].count("vpn-ipv4") == 1

    def test_no_rapid_update_afs_key_when_variants_have_none(self):
        intent = {"variant": {"rr": {"neighbors": []}}}
        result = compose_bgp(device_ctx={}, bgp_intent=intent)
        assert "rapid_update_afs" not in result

    def test_rapid_update_afs_absent_when_no_variants(self):
        result = compose_bgp(device_ctx={}, bgp_intent={})
        assert "rapid_update_afs" not in result

    def test_rapid_update_afs_is_sorted(self):
        intent = {
            "variant": {
                "a": {"rapid_update_afs": ["vpn-ipv6", "evpn", "vpn-ipv4"]},
            }
        }
        result = compose_bgp(device_ctx={}, bgp_intent=intent)
        assert result["rapid_update_afs"] == sorted(result["rapid_update_afs"])

    def test_other_keys_passed_through(self):
        intent = {"router_id": "10.0.0.1", "variant": {}}
        result = compose_bgp(device_ctx={}, bgp_intent=intent)
        assert result["router_id"] == "10.0.0.1"


# ---------------------------------------------------------------------------
# Pass-through composers (sr, evpn_esi, evpn_vpls, vprn)
# ---------------------------------------------------------------------------


class TestPassThroughComposers:
    def test_compose_sr_returns_intent_copy(self):
        intent = {"mpls_label": 100, "prefix_sid": "1.1.1.1"}
        result = compose_sr(device_ctx={}, sr_intent=intent)
        assert result == intent
        assert result is not intent  # is a copy

    def test_compose_evpn_esi_returns_intent_copy(self):
        intent = {"variant": {"default": {"interfaces": []}}}
        result = compose_evpn_esi(device_ctx={}, evpn_esi_intent=intent)
        assert result == intent
        assert result is not intent

    def test_compose_evpn_vpls_returns_intent_copy(self):
        intent = {"variant": {"ce_mgmt": {"evi_id": 11000}}}
        result = compose_evpn_vpls(device_ctx={}, evpn_vpls_intent=intent)
        assert result == intent
        assert result is not intent

    def test_compose_vprn_returns_intent_copy(self):
        intent = {"service_id": 42, "route_target": "64500:42"}
        result = compose_vprn(device_ctx={}, vprn_intent=intent)
        assert result == intent
        assert result is not intent


# ---------------------------------------------------------------------------
# compose_services dispatcher
# ---------------------------------------------------------------------------


class TestComposeServices:
    def test_empty_intent_returns_empty_dict(self):
        assert compose_services(device_ctx={}, service_intent={}) == {}

    def test_only_isis_present_produces_only_isis_key(self):
        ctx = _device_ctx()
        intent = {"isis": {"process_id": "1"}}
        result = compose_services(device_ctx=ctx, service_intent=intent)
        assert set(result.keys()) == {"isis"}

    def test_only_bgp_present_produces_only_bgp_key(self):
        intent = {"bgp": {"variant": {}}}
        result = compose_services(device_ctx={}, service_intent=intent)
        assert set(result.keys()) == {"bgp"}

    def test_sr_sr_evpn_esi_evpn_vpls_vprn_each_dispatched(self):
        intent = {
            "sr": {},
            "evpn_esi": {},
            "evpn_vpls": {},
            "vprn": {},
        }
        result = compose_services(device_ctx={}, service_intent=intent)
        assert set(result.keys()) == {"sr", "evpn_esi", "evpn_vpls", "vprn"}

    def test_unknown_service_key_is_silently_ignored(self):
        intent = {"unknown_service": {"foo": "bar"}}
        result = compose_services(device_ctx={}, service_intent=intent)
        assert result == {}

    def test_multiple_services_all_present_in_result(self):
        ctx = _device_ctx()
        intent = {
            "isis": {"process_id": "1"},
            "bgp": {"variant": {}},
            "sr": {},
        }
        result = compose_services(device_ctx=ctx, service_intent=intent)
        assert set(result.keys()) == {"isis", "bgp", "sr"}
