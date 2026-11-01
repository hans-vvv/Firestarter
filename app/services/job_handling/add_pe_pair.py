"""Job handler for the 'add_pe_pair' action — creates a redundant PE pair with inter-PE cabling."""

from __future__ import annotations

from typing import Any

from app.models.dataclass_models import DeviceSelectorView, RoleView
from app.repositories import (
    get_cables_between_devices,
    get_device_by_hostname,
    get_prefix_pool_by_name,
)
from app.services.job_handling.base import BaseActionJobHandler
from app.utils import require


class AddPEPairJobHandler(BaseActionJobHandler):
    """
    Job action handler for creating a PE pair:

    - Two PE devices (as a unit of work)
    - P2P link between them (IP + cable)
    - Shared label: pe-pair:<site>-<index>
    - Use of LAGs is enforced

    Idempotent:
    - On first run, creates devices/p2p connection/cable.
    - On subsequent runs return previous result
    """

    def identity(self, step: dict) -> str:
        """Generate a unique identity string for the job step based on hostnames."""
        a = step["params"]["hostname_a"]
        b = step["params"]["hostname_b"]
        return f"add_pe_pair {a}-{b}"

    def _create_pe_pair(self, params: dict) -> dict:
        """
        Create PE devices first.
        Then let pe_pair builder finish the rest (cable and ptp link)
        Returns creation status and relevant device/cable IDs.
        """

        view = DeviceSelectorView(
            hostname=params["hostname_a"],
            labels={"tenant": params["tenant"]},
            role=RoleView(name=params["role"]),
        )

        loop0_pool_name = self.executor.addressing_policy_resolver.resolve_loopback0_pool(view)

        loop0_pool = require(
            get_prefix_pool_by_name(self.session, loop0_pool_name),
            f"PrefixPool '{loop0_pool_name}' missing",
        )

        loop1_pool_name = self.executor.addressing_policy_resolver.resolve_loopback1_pool(view)

        loop1_pool = require(
            get_prefix_pool_by_name(self.session, loop1_pool_name),
            f"PrefixPool '{loop1_pool_name}' missing",
        )

        # Create devices
        dev_a = self.executor.device_builder.build_device(
            hostname=params["hostname_a"],
            model_name=params["model_name"],
            role_name=params["role"],
            site_name=params["site"],
            loopback0_pool=loop0_pool,
            loopback1_pool=loop1_pool,
            tenant=params["tenant"],
            ring=params.get("ring"),
        )

        dev_b = self.executor.device_builder.build_device(
            hostname=params["hostname_b"],
            model_name=params["model_name"],
            role_name=params["role"],
            site_name=params["site"],
            loopback0_pool=loop0_pool,
            loopback1_pool=loop1_pool,
            tenant=params["tenant"],
            ring=params.get("ring"),
        )

        p2p_pool_name = self.executor.addressing_policy_resolver.resolve_p2p_pool(dev_a, dev_b)

        p2p_pool = require(
            get_prefix_pool_by_name(self.session, p2p_pool_name),
            f"PrefixPool '{p2p_pool_name}' missing",
        )

        cable = self.executor.pe_pair_builder.create_pe_pair(
            on_lag=True,
            dev_a=dev_a,
            dev_b=dev_b,
            p2p_pool=p2p_pool,
        )

        return {
            "dev_a": dev_a.id,
            "dev_b": dev_b.id,
            "cable": cable.id,
            "status": "created",
            "topology_changed": True,
        }

    def handle(self, step: dict) -> dict:
        """
        Execute the job step, handling cases of existing devices and cables.

        Raises RuntimeError on inconsistent topology.

        Returns status dict.
        """
        params: dict[str, Any] = step["params"]

        host_a = params["hostname_a"]
        host_b = params["hostname_b"]

        dev_a = get_device_by_hostname(self.session, host_a)
        dev_b = get_device_by_hostname(self.session, host_b)

        count = sum(d is not None for d in (dev_a, dev_b))

        # --------------------------------------------------------------
        # CASE 1 — No devices exist → create everything
        # --------------------------------------------------------------
        if count == 0:
            return self._create_pe_pair(params)

        # --------------------------------------------------------------
        # CASE 2 — Exactly one device exists → topology is invalid
        # --------------------------------------------------------------
        if count == 1:
            raise RuntimeError(
                f"Inconsistent topology: Only one PE exists among "
                f"{host_a}, {host_b}. Expected none or both."
            )

        # --------------------------------------------------------------
        # CASE 3 — Both devices exist
        # --------------------------------------------------------------
        dev_a = require(dev_a, "Device A unexpectedly missing")
        dev_b = require(dev_b, "Device B unexpectedly missing")

        cables = get_cables_between_devices(self.session, dev_a, dev_b)

        if len(cables) > 0:
            # Pair fully provisioned → idempotent
            return {
                "dev_a": dev_a.id,
                "dev_b": dev_b.id,
                "cable": cables[0].id,
                "status": "exists",
            }

        # --------------------------------------------------------------
        # CASE 4 — Both devices exist BUT no cable → invalid state
        # --------------------------------------------------------------
        raise RuntimeError(
            f"Inconsistent topology: PEs {host_a} and {host_b} both exist "
            f"but no cable connects them. Manual correction required."
        )
