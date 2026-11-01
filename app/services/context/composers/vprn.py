"""VPRN context composer — transforms VPRN service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_vprn(
    *,
    device_ctx: dict[str, Any],
    vprn_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns device-level VPRN render context.

    """

    # Shallow copy is enough; intent is already device-scoped
    vprn_ctx = {k: v for k, v in vprn_intent.items()}

    return vprn_ctx
