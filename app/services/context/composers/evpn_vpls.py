"""EVPN VPLS context composer — transforms EVPN VPLS service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_evpn_vpls(
    *,
    device_ctx: dict[str, Any],
    evpn_vpls_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns device-level EVPN VPLS render context.

    """

    # Shallow copy is enough; intent is already device-scoped
    evpn_vpls_ctx = {k: v for k, v in evpn_vpls_intent.items()}

    return evpn_vpls_ctx
