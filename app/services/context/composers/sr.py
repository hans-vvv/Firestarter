"""Segment Routing context composer — transforms SR service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_sr(
    *,
    device_ctx: dict[str, Any],
    sr_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns device-level SR render context.

    """

    # Shallow copy is enough; intent is already device-scoped
    sr_ctx = {k: v for k, v in sr_intent.items()}

    return sr_ctx
