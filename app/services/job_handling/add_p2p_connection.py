"""Job handler for the 'add_p2p_connection' action — cables two devices and allocates link IPs."""

from __future__ import annotations

from app.repositories import (
    get_cables_between_devices,
    get_device_by_hostname,
    get_prefix_pool_by_name,
)
from app.services.job_handling.base import BaseActionJobHandler
from app.utils import require


class AddP2PConnectionJobHandler(BaseActionJobHandler):
    """
    Handles the job to add a point-to-point connection between two devices,
    also a cable object is added between the devices

    Ensures idempotency by checking for existing cables, uses a specified prefix pool
    to allocate IPs.

    Notes:
    - The use of LAGs on NNI links is enforce.
    - One P2P connection between two devices is supported and no job type
      is available (yet?) to add an extra cable to an existing LAG
    """

    def identity(self, step: dict) -> str:
        """Generate a unique identity string for the job step based on device names."""
        a = step["params"]["device_a_name"]
        b = step["params"]["device_b_name"]
        return f"add_cable {a}-{b}"

    def handle(self, step: dict) -> dict:
        """
        Execute the job step to add a cable.

        Checks device presence and existing cables for idempotency,
        creates a new p2p connection if needed.

        Returns a dict describing the cable creation status.
        """

        params = step["params"]

        dev_a = get_device_by_hostname(self.session, params["device_a_name"])
        dev_b = get_device_by_hostname(self.session, params["device_b_name"])

        # ----------------------------------------------------------
        # IDEMPOTENCE: Check if cable already exists between devices
        # ----------------------------------------------------------
        count = sum(d is not None for d in (dev_a, dev_b))

        if count == 0 or count == 1:
            raise RuntimeError(
                f"Inconsistent topology: Only zero or one PE exists "
                f"{params['device_a_name']}, {params['device_b_name']}. Expected both."
            )

        if count == 2:
            dev_a = require(dev_a, "Device A unexpectedly missing")
            dev_b = require(dev_b, "Device B unexpectedly missing")

            # Include retired cables in the idempotency check. A retired
            # cable for this pair means a ring migration took the link
            # out of service on purpose: the Excel sheet still mentions
            # it (the migration tool does not edit Excel), but the
            # operator's authoritative intent is recorded in the DB.
            # Treating the retired row as "exists" prevents the
            # pipeline from trying to rebuild a cable on an interface
            # that has since been re-allocated to a different link.
            cables = get_cables_between_devices(
                self.session,
                dev_a,
                dev_b,
                exclude_retired=False,
            )
            if len(cables) > 0:
                # Pair fully provisioned (or intentionally retired) → idempotent.
                return {
                    "cable": cables[0].id,
                    "status": "exists",
                }

        dev_a = require(
            get_device_by_hostname(self.session, params["device_a_name"]),
            f"Device with name {params['device_a_name']} must be created first",
        )
        dev_b = require(
            get_device_by_hostname(self.session, params["device_b_name"]),
            f"Device with name {params['device_b_name']} must be created first",
        )

        p2p_pool_name = self.executor.addressing_policy_resolver.resolve_p2p_pool(dev_a, dev_b)

        p2p_pool = require(
            get_prefix_pool_by_name(self.session, p2p_pool_name),
            f"PrefixPool '{p2p_pool_name}' missing",
        )
        cable = self.executor.topology_builder.build_p2p_link(
            dev_a_name=params["device_a_name"],
            dev_b_name=params["device_b_name"],
            pool=p2p_pool,
            iface_a_name=params.get("iface_a_name"),
            iface_b_name=params.get("iface_b_name"),
            on_lag=True,
        )
        return {"cable_id": cable.id, "status": "created", "topology_changed": True}
