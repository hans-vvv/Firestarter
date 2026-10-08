"""EVPN ESI context composer — transforms EVPN ESI service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_evpn_esi(
    *,
    device_ctx: dict[str, Any],
    evpn_esi_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns device-level EVPN EVI render context.

    """

    # Shallow copy is enough; intent is already device-scoped
    evpn_esi_ctx = {k: v for k, v in evpn_esi_intent.items()}

    return evpn_esi_ctx
