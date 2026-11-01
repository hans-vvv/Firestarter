"""BGP context composer — transforms BGP service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_bgp(
    *,
    device_ctx: dict[str, Any],
    bgp_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns device-level BGP render context.
    """

    bgp_ctx = {k: v for k, v in bgp_intent.items()}

    # Merge Rapid update AFs from multiple variants
    all_afs: set[str] = set()
    for v in bgp_ctx.get("variant", {}).values():
        for af in v.get("rapid_update_afs", []):
            all_afs.add(af)

    if all_afs:
        bgp_ctx["rapid_update_afs"] = sorted(all_afs)

    return bgp_ctx
