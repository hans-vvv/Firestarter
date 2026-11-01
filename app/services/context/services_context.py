"""Merges per-protocol service context into the final per-device render context."""

from __future__ import annotations

from typing import Any

from app.services.context.composers import (
    compose_bgp,
    compose_evpn_esi,
    compose_evpn_vpls,
    compose_isis,
    compose_sr,
    compose_vprn,
)

# from app.utils import jprint


def compose_services(
    *,
    device_ctx: dict,
    service_intent: dict,
) -> dict:
    """
    Decorate device render context with service intent.

    Assumptions:
    - device_ctx already contains fully composed interfaces
    - service_intent is device-scoped
    """

    result: dict[str, Any] = {}

    if "isis" in service_intent:
        result["isis"] = compose_isis(
            device_ctx=device_ctx,
            isis_intent=service_intent["isis"],
        )

    if "bgp" in service_intent:
        result["bgp"] = compose_bgp(
            device_ctx=device_ctx,
            bgp_intent=service_intent["bgp"],
        )

    if "sr" in service_intent:
        result["sr"] = compose_sr(
            device_ctx=device_ctx,
            sr_intent=service_intent["sr"],
        )

    if "evpn_esi" in service_intent:
        result["evpn_esi"] = compose_evpn_esi(
            device_ctx=device_ctx,
            evpn_esi_intent=service_intent["evpn_esi"],
        )

    if "vprn" in service_intent:
        result["vprn"] = compose_vprn(
            device_ctx=device_ctx,
            vprn_intent=service_intent["vprn"],
        )

    if "evpn_vpls" in service_intent:
        result["evpn_vpls"] = compose_evpn_vpls(
            device_ctx=device_ctx,
            evpn_vpls_intent=service_intent["evpn_vpls"],
        )

    return result
