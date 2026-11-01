"""BGP feature handler — computes per-device BGP neighbour context."""

from __future__ import annotations

from typing import Any

from app.repositories import get_all_devices, get_loopback_ip_from_device
from app.utils import Tree

from .base import BaseFeatureHandler


class BGPFeatureHandler(BaseFeatureHandler):
    """
    Builds iBGP peering relationships using declarative selectors.

    Supports:
      - RR / RR-client topologies
      - Full-mesh iBGP
      - Arbitrary device groups via selectors
    """

    def compute(self, svc_ctx: dict[str, Any]):
        service_data = svc_ctx["service_data"]

        bgp_cfg: dict[str, Any] = service_data["features"]["bgp"]
        variant: str = service_data["variant"]

        asn: int = bgp_cfg["asn"]
        mode: str = bgp_cfg["rr"]["mode"]
        pg_rr: str = bgp_cfg["rr"]["rr_peer_group_name"]
        pg_rrc: str = bgp_cfg["rr"]["rrc_peer_group_name"]

        bgp_afs_per_role: dict[str, list[str]] = service_data["parameters"]["bgp_afs"]["role"]
        pg_suffix_per_role: dict[str, str] = service_data["parameters"]["bgp_afs"]["pg_suffix"]

        # Helpers to calculate rapid update address families per device.
        def require_role_mapping(role: str, *, hostname: str) -> tuple[list[str], str]:
            if role not in bgp_afs_per_role:
                raise ValueError(f"BGP AF mapping missing for role='{role}' (device='{hostname}')")
            if role not in pg_suffix_per_role:
                raise ValueError(
                    f"BGP pg_suffix mapping missing for role='{role}' (device='{hostname}')"
                )
            return bgp_afs_per_role[role], pg_suffix_per_role[role]

        def pg_name(base: str, role: str, *, hostname: str) -> tuple[str, list[str]]:
            afs, suffix = require_role_mapping(role, hostname=hostname)
            return f"{base}_{suffix}", afs

        def rapid_update_afs_from_peer_groups(peer_groups: dict[str, dict[str, Any]]) -> list[str]:
            afs: set[str] = set()
            for pg in peer_groups.values():
                for af in pg.get("afs", []):
                    if af != "route-target":
                        afs.add(af)
            return sorted(afs)

        context = Tree()

        all_devices = get_all_devices(self.session)
        sel_bgp_devices: dict[str, Any] = service_data["selectors"]["devices"]["bgp"]
        sel_rr_devices: dict[str, Any] = service_data["selectors"]["devices"]["route_reflectors"]

        bgp_devices = self.sb.selector_engine.select(all_devices, sel_bgp_devices)
        rr_devices = self.sb.selector_engine.select(all_devices, sel_rr_devices)

        if not bgp_devices or not rr_devices:
            raise ValueError(
                f"BGP selector matched 0 devices or RRs for tenant={service_data['tenant']}"
            )

        # ============================================================
        # CASE 1 — FULL MESH iBGP
        # ============================================================
        if mode == "fullmesh":
            for dev in bgp_devices:
                peers = []

                for peer in bgp_devices:
                    if peer.id == dev.id:
                        continue

                    lo = get_loopback_ip_from_device(self.session, peer, 0)
                    if not lo:
                        continue

                    peers.append(
                        {
                            "neighbor_ip": lo.address.split("/")[0],
                            "remote_as": asn,
                            "peer_group": "FULLMESH",
                            "neighbor_hostname": peer.hostname,
                        }
                    )

                context[dev.hostname]["bgp"]["variant"][variant] = {
                    "asn": asn,
                    "peer_group": "FULLMESH",
                    "neighbors": peers,
                }

            return context

        # ============================================================
        # CASE 2 — EXPLICIT ROUTE REFLECTOR TOPOLOGY
        # ============================================================
        for dev in bgp_devices:
            is_rr = dev in rr_devices

            # --------------------------------------------------------
            # RR BEHAVIOR
            # --------------------------------------------------------
            if is_rr:
                peers = []
                peer_groups: dict[str, dict[str, Any]] = {}

                for peer in bgp_devices:
                    if peer.id == dev.id:
                        continue
                    if peer in rr_devices:
                        continue  # no RR-RR sessions

                    # TODO: use require helper.
                    lo = get_loopback_ip_from_device(self.session, peer, 0)
                    if not lo:
                        continue

                    peer_role = peer.role.name
                    pg, afs = pg_name(pg_rr, peer_role, hostname=dev.hostname)

                    # define/dedupe group on this device
                    peer_groups.setdefault(pg, {"afs": afs})

                    peers.append(
                        {
                            "neighbor_ip": lo.address.split("/")[0],
                            "remote_as": asn,
                            "peer_group": pg,
                            "neighbor_hostname": peer.hostname,
                        }
                    )

                context[dev.hostname]["bgp"]["variant"][variant] = {
                    "asn": asn,
                    "topology_role": "rr",
                    "peer_groups": peer_groups,
                    "neighbors": peers,
                    "rapid_update_afs": rapid_update_afs_from_peer_groups(peer_groups),
                }

            # --------------------------------------------------------
            # CLIENT BEHAVIOR
            # --------------------------------------------------------
            else:
                peers = []
                peer_groups: dict[str, dict[str, Any]] = {}

                dev_role = dev.role.name
                pg, afs = pg_name(pg_rrc, dev_role, hostname=dev.hostname)

                # define exactly once per client (dedupe anyway)
                peer_groups.setdefault(pg, {"afs": afs})

                for rr in rr_devices:
                    lo = get_loopback_ip_from_device(self.session, rr, 0)
                    if not lo:
                        continue

                    peers.append(
                        {
                            "neighbor_ip": lo.address.split("/")[0],
                            "remote_as": asn,
                            "peer_group": pg,
                            "neighbor_hostname": rr.hostname,
                        }
                    )

                context[dev.hostname]["bgp"]["variant"][variant] = {
                    "asn": asn,
                    "topology_role": "client",
                    "peer_groups": peer_groups,
                    "neighbors": peers,
                    "rapid_update_afs": rapid_update_afs_from_peer_groups(peer_groups),
                }

        return dict(context)
