"""IS-IS context composer — transforms IS-IS service intent into template-ready dicts."""

from __future__ import annotations

from typing import Any


def compose_isis(
    *,
    device_ctx: dict[str, Any],
    isis_intent: dict[str, Any],
) -> dict[str, Any]:
    """
    Attach ISIS configuration to existing interface render units
    and produce device-level ISIS context.
    """

    # Device-level ISIS context:
    # everything except interface list
    isis_ctx = {k: v for k, v in isis_intent.items() if k != "interfaces"}

    # Build lookup table for interfaces by name
    iface_index: dict[str, Any] = {iface["name"]: iface for iface in device_ctx["interfaces"]}

    # Attach ISIS to interfaces. Works because dicts are
    # passed by reference.
    for iface_cfg in sorted(
        isis_intent.get("interfaces", []),
        key=lambda x: x["iface_name"],
    ):
        iface_name = iface_cfg["iface_name"]

        iface = iface_index.get(iface_name)
        if iface is None:
            raise RuntimeError(
                f"ISIS enabled on unknown interface '{iface_name}' "
                f"on device {device_ctx['hostname']}"
            )

        # Attach ISIS block to interface render unit
        iface["isis"] = {k: v for k, v in iface_cfg.items() if k != "iface_name"}

    # print(isis_ctx)

    return isis_ctx
