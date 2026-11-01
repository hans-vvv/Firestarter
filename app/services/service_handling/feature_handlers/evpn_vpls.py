"""EVPN VPLS feature handler — computes per-device EVPN VPLS (L2VPN) context."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Device, Interface
from app.repositories import get_all_devices
from app.utils import Tree

from .base import BaseFeatureHandler


def pair_label_for_device(device: Device) -> str:
    """Normalised pair label used as the per-pair resource allocation key.

    A paired PE carries ``labels["pair_label"] = "pe-pair:<site>-1"``; the part
    after the colon keys the shared EVI allocation so both members of the pair
    land on the same EVI. A standalone device falls back to its hostname.
    """
    raw = device.labels.get("pair_label") or device.hostname
    return raw.split(":", 1)[-1]


class EVPN_VPLSFeatureHandler(BaseFeatureHandler):
    def compute(self, svc_ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Computes on which PE interfaces connected to L2 CEs l2vpn config must be applied.

        Two attachment models are supported, resolved per vpls_cfg by _attachment_model():

          declared        — att_circuits defined in YAML (per-VPLS or shared); EVI and S-VLAN
                            derived directly from the VPLS name suffix.
          topology_driven — no declared circuits; DB-discovered CE-facing interfaces, EVI
                            allocated from pool.

        """
        context = Tree()

        service_data: dict[str, Any] = svc_ctx["service_data"]
        parameters: dict[str, Any] = service_data["parameters"]
        vpls_cfgs: list[dict[str, Any]] = parameters["vplss"]
        shared_att_circuits: list[dict[str, Any]] = parameters.get("att_circuits", [])

        service_name: str = service_data["service"]
        variant: str = service_data["variant"]
        allocation_base = service_name + "_" + variant

        bgp_route_target_prefix: str | None = parameters.get("bgp_route_target_prefix")

        all_devices = get_all_devices(self.session)
        sel_evpn_vpls_devices: dict[str, Any] = service_data["selectors"]["devices"]["evpn_vpls"]
        evpn_vpls_devices = self.sb.selector_engine.select(all_devices, sel_evpn_vpls_devices)

        # No PE devices present in intial production building phase
        # if not evpn_vpls_devices:
        #     raise ValueError(
        #         f"EVPN VPLS selector matched 0 devices for tenant={tenant} variant={variant}"
        #     )

        interfaces = list(
            self.session.scalars(
                select(Interface)
                .join(Interface.device)
                .where(Interface.evpn_esi.is_not(None))
                .options(selectinload(Interface.device))
            )
        )

        for iface in interfaces:
            if iface.evpn_esi == "needs esi":
                raise RuntimeError("Underlay not ready to compute evpn_vpls service")

        ifaces_by_device: dict[str, list[Interface]] = defaultdict(list)
        for iface in interfaces:
            ifaces_by_device[iface.device.hostname].append(iface)

        for device in evpn_vpls_devices:
            pair_label = pair_label_for_device(device)

            for vpls_cfg in vpls_cfgs:
                model = self._attachment_model(vpls_cfg, shared_att_circuits)

                if model == "declared":
                    self._handle_declared(
                        device=device,
                        vpls_cfg=vpls_cfg,
                        variant=variant,
                        shared_att_circuits=shared_att_circuits,
                        ifaces_by_device=ifaces_by_device,
                        bgp_route_target_prefix=bgp_route_target_prefix,
                        context=context,
                    )
                else:  # topology_driven
                    self._handle_topology_driven(
                        device=device,
                        vpls_cfg=vpls_cfg,
                        variant=variant,
                        shared_att_circuits=shared_att_circuits,
                        ifaces_by_device=ifaces_by_device,
                        allocation_base=allocation_base,
                        bgp_route_target_prefix=bgp_route_target_prefix,
                        pair_label=pair_label,
                        context=context,
                    )

        return dict(context)

    # ------------------------------------------------------------------ #
    # Model detection                                                      #
    # ------------------------------------------------------------------ #

    def _attachment_model(
        self,
        vpls_cfg: dict[str, Any],
        shared_att_circuits: list[dict[str, Any]],
    ) -> str:
        """
        Determine which attachment model applies to this VPLS config.

          declared        — att_circuits defined in YAML (per-VPLS or shared)
          topology_driven — no declared circuits; fall back to DB-discovered interfaces
        """
        if vpls_cfg.get("att_circuits") is not None or shared_att_circuits:
            return "declared"
        return "topology_driven"

    # ------------------------------------------------------------------ #
    # Attachment model handlers                                            #
    # ------------------------------------------------------------------ #

    def _handle_declared(
        self,
        *,
        device: Device,
        vpls_cfg: dict[str, Any],
        variant: str,
        shared_att_circuits: list[dict[str, Any]],
        ifaces_by_device: dict[str, list[Interface]],
        bgp_route_target_prefix: str | None,
        context: Tree,
    ) -> None:
        """
        Declared att_circuits model: attachment circuits defined in YAML.
        EVI and S-VLAN are derived from the VPLS name suffix (DC convention).
        """
        vpls_name: str = vpls_cfg["name"]

        interfaces_ctx = self._build_att_circuits_context(
            device=device,
            vpls_cfg=vpls_cfg,
            shared_att_circuits=shared_att_circuits,
            ifaces_by_device=ifaces_by_device,
        )

        vpls_ctx = context[device.hostname]["evpn_vpls"]["variant"][variant][vpls_name]
        vpls_ctx["interfaces"] = interfaces_ctx
        vpls_ctx["evi_id"] = self._derive_evi_id_from_vpls_name(vpls_name)
        vpls_ctx["s_vlan"] = self._derive_s_vlan_from_vpls_name(vpls_name)
        vpls_ctx["vpls_name"] = vpls_name
        vpls_ctx["qos_policy_name"] = vpls_cfg.get("qos_policy_name")
        vpls_ctx["routed_vpls"] = vpls_cfg.get("routed_vpls", False)
        vpls_ctx["split_horizon_group_name"] = vpls_cfg.get("split_horizon_group_name")
        vpls_ctx["bgp_route_target_prefix"] = bgp_route_target_prefix

    def _handle_topology_driven(
        self,
        *,
        device: Device,
        vpls_cfg: dict[str, Any],
        variant: str,
        shared_att_circuits: list[dict[str, Any]],
        ifaces_by_device: dict[str, list[Interface]],
        allocation_base: str,
        bgp_route_target_prefix: str | None,
        pair_label: str,
        context: Tree,
    ) -> None:
        """
        Topology-driven model: DB-discovered CE-facing interfaces, EVI allocated from pool.
        """
        vpls_name: str = vpls_cfg["name"]

        interfaces_ctx = self._build_att_circuits_context(
            device=device,
            vpls_cfg=vpls_cfg,
            shared_att_circuits=shared_att_circuits,
            ifaces_by_device=ifaces_by_device,
        )

        resource_pool_name: str = vpls_cfg["service_id_pool"]["name"]
        pool_allocations = self.sb.rpa.allocate_per_service_instance(
            allocation_name=allocation_base + "_" + pair_label + "_" + vpls_name,
            allocations={pair_label: resource_pool_name},
        )
        evi_id = pool_allocations[pair_label]

        vpls_ctx = context[device.hostname]["evpn_vpls"]["variant"][variant][vpls_name]
        vpls_ctx["interfaces"] = interfaces_ctx
        vpls_ctx["evi_id"] = evi_id
        vpls_ctx["qos_policy_name"] = vpls_cfg.get("qos_policy_name")
        vpls_ctx["s_vlan"] = vpls_cfg.get("s_vlan")
        vpls_ctx["vpls_name"] = vpls_name
        vpls_ctx["routed_vpls"] = vpls_cfg.get("routed_vpls", False)
        vpls_ctx["split_horizon_group_name"] = vpls_cfg.get("split_horizon_group_name")
        vpls_ctx["bgp_route_target_prefix"] = bgp_route_target_prefix

    # ------------------------------------------------------------------ #
    # Shared helpers                                                       #
    # ------------------------------------------------------------------ #

    def _build_att_circuits_context(
        self,
        *,
        device: Device,
        vpls_cfg: dict[str, Any],
        shared_att_circuits: list[dict[str, Any]],
        ifaces_by_device: dict[str, list[Interface]],
    ) -> list[dict[str, Any]]:
        """
        Resolve attachment circuits for a VPLS.

        Precedence:
        1. per-VPLS att_circuits (vpls_cfg)
        2. shared att_circuits
        3. DB-discovered interfaces
        """
        vpls_att_circuits = vpls_cfg.get("att_circuits")

        if vpls_att_circuits is not None:
            return [
                {
                    "iface_name": att_circuit["name"],
                    "render_type": att_circuit.get("render_type"),
                    "is_synthetic": True,
                }
                for att_circuit in vpls_att_circuits
            ]

        if shared_att_circuits:
            return [
                {
                    "iface_name": att_circuit["name"],
                    "render_type": att_circuit.get("render_type"),
                    "is_synthetic": True,
                }
                for att_circuit in shared_att_circuits
            ]

        return [
            {
                "iface_name": iface.name,
                "is_synthetic": False,
            }
            for iface in ifaces_by_device.get(device.hostname, [])
        ]

    def _derive_evi_id_from_vpls_name(self, vpls_name: str) -> int:
        """
        Extract EVI/service ID from VPLS name suffix (DC convention).
        Example: INB-MGMT-1200 -> 1200
        """
        return int(vpls_name.split("-")[-1])

    def _derive_s_vlan_from_vpls_name(self, vpls_name: str) -> str:
        """
        Derive S-VLAN from VPLS name suffix (DC convention).
        Example: INB-MGMT-1200 -> 120
        """
        return str(int(vpls_name.split("-")[-1]) // 10)
