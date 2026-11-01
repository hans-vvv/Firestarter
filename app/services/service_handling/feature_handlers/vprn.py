"""VPRN feature handler — computes per-device L3VPN context including VRF and SAP configuration."""

from __future__ import annotations

import ipaddress
from typing import Any

from app.domain.ce_mgmt import ce_mgmt_allocation_name
from app.models import Device
from app.repositories.device import get_all_devices
from app.repositories.ip_address import get_loopback_ip_from_device
from app.repositories.prefix_pool import get_prefix_pool_by_name
from app.utils import Tree, require

from .base import BaseFeatureHandler


class VPRNFeatureHandler(BaseFeatureHandler):
    """
    Build device-scoped VPRN context from declarative service selectors.

    Multiple VPRNs are supported through:

        parameters:
          vprns: [...]

    For each selected device, the handler creates per-variant VPRN service data.
    When DHCP server settings are present for a VPRN, the handler also allocates
    an address prefix, derives DHCP pool parameters, and links the VPRN to the
    corresponding VPLS service allocation.

    ``subnet_info`` describes an IRB subnet the VPRN exposes towards CEs through
    a routed VPLS (the VPLS is named after the VPRN — see
    ``_derive_vpls_name_from_vprn_name``). Its keys:

    - ``prefix_pool_name`` + ``prefixlen``: allocate one delegated prefix per PE
      pair from that pool (``delegated`` pool type); or ``prefix``: use a fixed
      subnet on every device.
    - ``ce_mgmt: true``: marks this VPRN as *the* CE-management VPRN. Its
      delegated prefixes are stored under the shared ``ce_mgmt_<pair_label>``
      name (``app.domain.ce_mgmt``) so the CE management allocator can hand
      every CE a sticky address inside them. Exactly one VPRN in the whole
      service set may carry this flag; two would silently share one subnet.
    """

    def compute(self, svc_ctx: dict[str, Any]) -> dict[str, Any]:
        """
        Compose VPRN render context for all selected devices.

        Parameters
        ----------
        svc_ctx : dict[str, Any]
            Service build context containing `service_data`.

        Returns
        -------
        dict[str, Any]
            Nested device-scoped VPRN context.
        """
        service_data: dict[str, Any] = svc_ctx["service_data"]
        variant: str = service_data["variant"]

        device_selector: dict[str, Any] = service_data["selectors"]["devices"]["vprn"]
        vprn_cfgs: list[dict[str, Any]] = service_data["parameters"]["vprns"]
        import_target_names: dict[str, str] = service_data["parameters"].get(
            "import_target_names", {}
        )

        allocation_base = self._allocation_base(service_data)

        context = Tree()

        vprn_devices = self._select_vprn_devices(device_selector=device_selector)

        # No PE devices present during initial production building
        # if not vprn_devices:
        #     raise ValueError(
        #         f"VPRN selector matched 0 devices for tenant={tenant} variant={variant}"
        #     )

        for device in vprn_devices:
            pair_label = device.labels.get("pair_label") or device.hostname

            for vprn_cfg in vprn_cfgs:
                vprn_name: str = vprn_cfg["name"]
                vprn_ctx = self._build_base_vprn_context(
                    vprn_cfg=vprn_cfg,
                    import_target_names=import_target_names,
                )

                dhcp_cfg: dict[str, Any] | None = vprn_cfg.get("dhcp_server")
                if dhcp_cfg:
                    vprn_ctx.update(
                        self._build_dhcp_context(
                            dhcp_cfg=dhcp_cfg,
                            allocation_base=allocation_base,
                            pair_label=pair_label,
                            device=device,
                            vprn_name=vprn_name,
                        )
                    )

                mgmt_cfg: dict[str, Any] | None = vprn_cfg.get("mgmt_vprn_info")
                if mgmt_cfg:
                    vprn_ctx.update(
                        self._build_mgmt_ctx(
                            device=device,
                            mgmt_cfg=mgmt_cfg,
                        )
                    )

                subnet_cfg: dict[str, Any] | None = vprn_cfg.get("subnet_info")
                if subnet_cfg:
                    vprn_ctx.update(
                        self._build_subnet_context(
                            subnet_cfg=subnet_cfg,
                            device=device,
                            allocation_base=allocation_base,
                            pair_label=pair_label,
                            vprn_name=vprn_name,
                        )
                    )

                context[device.hostname]["vprn"]["variant"][variant][vprn_name] = vprn_ctx

        return dict(context)

    def _select_vprn_devices(self, *, device_selector: dict[str, Any]) -> list[Device]:
        """
        Select devices targeted for VPRN configuration.
        """
        all_devices = get_all_devices(self.session)
        return self.sb.selector_engine.select(all_devices, device_selector)

    def _allocation_base(self, service_data: dict[str, Any]) -> str:
        """
        Build the base allocation name used for DHCP-related reservations.
        """
        service_name: str = service_data["service"]
        variant: str = service_data["variant"]
        return f"{service_name}_{variant}"

    def _build_base_vprn_context(
        self,
        *,
        vprn_cfg: dict[str, Any],
        import_target_names: dict[str, str],
    ) -> dict[str, Any]:
        """
        Build the base render context for a single VPRN definition.

        When ``use_policy`` is set, the SR OS template names a policy-options
        community per imported route-target by looking the RT up in
        ``import_target_names`` (see ``sros/vprn.j2``). A missing entry is not
        caught here today: it renders fine right up until Jinja evaluates
        ``import_target_names[rt]`` and raises ``UndefinedError`` — three stages
        downstream, during a compliance/print render, with no hint of which
        VPRN or RT is at fault. Worse, a plain pipeline run never renders, so it
        reports success and the broken intent only surfaces later. Validate the
        invariant here, at compute time, so a missing name fails the pipeline
        run itself with the exact VPRN and RT named.
        """
        if vprn_cfg["use_policy"]:
            missing = [rt for rt in vprn_cfg["rt_import"] if rt not in import_target_names]
            if missing:
                raise ValueError(
                    f"VPRN {vprn_cfg['name']!r} imports route-target(s) "
                    f"{missing} with no matching entry in "
                    f"parameters.import_target_names — add each RT there with "
                    f'the community name to use (e.g. "{missing[0]}": SOME-NAME).'
                )

        service_id = self._get_service_id_from_vprn_name(vprn_cfg["name"])

        return {
            "service_id": service_id,
            "rt_import": vprn_cfg["rt_import"],
            "rt_export": vprn_cfg["rt_export"],
            "use_policy": vprn_cfg["use_policy"],
            "import_target_names": import_target_names,
        }

    def _build_mgmt_ctx(
        self,
        *,
        device: Device,
        mgmt_cfg: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Build the base render context if VPRN is management VPRN.
        """
        mgmt_interface = mgmt_cfg["interface_name"]
        mgmt_ip = get_loopback_ip_from_device(session=self.session, device=device, loop_index=1)
        mgmt_ip = require(mgmt_ip, f"No Mgmt IP record found for {device.hostname}")
        return {
            "mgmt_interface": mgmt_interface,
            "mgmt_ip": mgmt_ip.address,
        }

    def _build_subnet_context(
        self,
        *,
        subnet_cfg: dict[str, Any],
        allocation_base: str,
        device: Device,
        pair_label: str,
        vprn_name: str,
    ) -> dict[str, Any]:
        """
        Build Subnet-specific context for a VPRN.

        This includes:
        - delegated prefix allocation or given prefix       -
        - IRB address
        - GW address
        """
        # If these are present: allocate from pool
        prefix_pool_name: str | None = subnet_cfg.get("prefix_pool_name")
        prefixlen: int | None = subnet_cfg.get("prefixlen")

        vpls_name = self._derive_vpls_name_from_vprn_name(vprn_name=vprn_name)

        # Derive prefix from allocation
        if prefix_pool_name is not None and prefixlen is not None:
            pool = require(
                get_prefix_pool_by_name(session=self.session, name=prefix_pool_name),
                f"No db record found for {prefix_pool_name}",
            )

            # The CE-management VPRN's prefixes are keyed by a name the CE
            # management allocator knows, not by this VPRN's service/variant.
            if subnet_cfg.get("ce_mgmt"):
                allocation_name = ce_mgmt_allocation_name(pair_label=pair_label)
            else:
                allocation_name = f"{allocation_base}_{pair_label}"

            delegated_prefix = self.sb.rpa.allocate_delegated_prefix_per_service_instance(
                allocation_name=allocation_name,
                pool=pool,
                prefixlen=prefixlen,
            )

            prefix = delegated_prefix["prefix"]

        else:
            prefix: str = subnet_cfg["prefix"]
            prefixlen = int(prefix.split("/")[-1])

        irb_ip = self._resolve_irb_ip_address(device=device, prefix=prefix)
        network = ipaddress.ip_network(prefix, strict=True)
        gw_ip = str(network.network_address + 1)

        return {
            "subnet": prefix,
            "prefixlen": prefixlen,
            "irb_interface_name": vpls_name,
            "irb_address": irb_ip,
            "gw_address": gw_ip,
            "vpls_name": vpls_name,
        }

    def _build_dhcp_context(
        self,
        *,
        dhcp_cfg: dict[str, Any],
        allocation_base: str,
        pair_label: str,
        device: Device,
        vprn_name: str,
    ) -> dict[str, Any]:
        """
        Build DHCP-specific context for a VPRN.

        This includes:
        - delegated prefix allocation
        - DHCP GI and pool layout        -
        """
        prefix_pool_name: str = dhcp_cfg["prefix_pool_name"]
        prefixlen: int = dhcp_cfg["prefixlen"]
        vpls_name = self._derive_vpls_name_from_vprn_name(vprn_name=vprn_name)

        pool = require(
            get_prefix_pool_by_name(session=self.session, name=prefix_pool_name),
            f"No db record found for {prefix_pool_name}",
        )

        prefix = self.sb.rpa.allocate_delegated_prefix_per_service_instance(
            allocation_name=f"{allocation_base}_{pair_label}",
            pool=pool,
            prefixlen=prefixlen,
        )

        gi, pool_start, pool_end = self._dhcp_layout(prefix=prefix["prefix"], device=device)

        irb_ip = self._resolve_irb_ip_address(device=device, prefix=prefix["prefix"])

        return {
            "gi": gi,
            "pool_start": pool_start,
            "pool_end": pool_end,
            "subnet": prefix["prefix"],
            "prefixlen": prefixlen,
            "irb_ip": irb_ip,
            "irb_interface_name": vpls_name,
            "dhcp_local_server_name": dhcp_cfg["dhcp_local_server_name"],
            "dhcp_local_server_address": dhcp_cfg["dhcp_local_server_address"],
            "dhcp_poolname": dhcp_cfg["dhcp_poolname"],
            "dhcp_option_125_string": dhcp_cfg["dhcp_option_125_string"],
            "dhcp_option_42_string": dhcp_cfg["dhcp_option_42_string"],
            "loopback_interface_name": dhcp_cfg["loopback_interface_name"],
            "vpls_name": vpls_name,
        }

    def _pair_position(self, device: Device) -> int:
        """
        Return ``0`` or ``1``: the device's position within its PE pair.

        Both members of a pair share ``labels["pair_label"]`` (set by the PE-pair
        builder); the member with the lexicographically smaller hostname is
        position ``0``, the other ``1``. A standalone PE is position ``0``. Every
        per-device offset inside a pair-shared subnet (the IRB address, the DHCP
        pool split) derives from this, so the two members of an A/A pair never
        claim the same addresses — without pinning any hostname convention.
        """
        pair_label = device.labels.get("pair_label")
        if not pair_label:
            return 0
        members = sorted(
            d.hostname
            for d in get_all_devices(self.session)
            if d.labels.get("pair_label") == pair_label
        )
        return members.index(device.hostname)

    def _dhcp_layout(self, device: Device, prefix: str) -> tuple[str, str, str]:
        """
        Compute the DHCP gi address and address-pool boundaries for an IPv4 subnet.

        The two members of a PE pair each run their own DHCP server on the shared
        subnet and those servers are not synchronised, so their pools must not
        overlap: the first member leases the lower half, the second the upper.

        Parameters
        ----------
        prefix : str
            IPv4 network in CIDR notation.
        device: Device

        Returns
        -------
        tuple[str, str, str]
            A tuple containing:
            - gateway interface address (first usable IP)
            - DHCP pool start address
            - DHCP pool end address

        Assumes a /28 prefix length: ``.1`` is the VRRP gateway, ``.2``/``.3``
        the two IRBs, ``.4`` to ``.8`` the first member's pool and ``.9`` to ``.14``
        the second member's.
        """
        net = ipaddress.ip_network(prefix, strict=True)
        hosts = list(net.hosts())

        if len(hosts) < 9:
            raise ValueError("Prefix too small for GI + two DHCP pools")

        gi = hosts[0]

        if self._pair_position(device) == 0:
            pool_start = hosts[3]
            pool_end = hosts[7]
        else:
            pool_start = hosts[8]
            pool_end = hosts[-1]

        return str(gi), str(pool_start), str(pool_end)

    def _resolve_irb_ip_address(self, device: Device, prefix: str):
        """
        Resolve the IRB address of ``device`` inside ``prefix``.

        ``.1`` is the passive-VRRP gateway shared by an A/A pair; the first pair
        member takes ``.2`` and the second ``.3`` (see ``_pair_position``). A
        standalone PE takes ``.2``.
        """
        network = ipaddress.ip_network(prefix, strict=True)
        host_offset = 2 + self._pair_position(device)

        irb_ip = str(network.network_address + host_offset)

        if irb_ip == str(network.broadcast_address):
            raise ValueError(f"Prefix {network} is too small for host offset {host_offset}")

        return irb_ip

    def _derive_vpls_name_from_vprn_name(self, vprn_name: str) -> str:
        """
        General implemented business logic:
        Example: CUSTOMER-A-20 -> CUSTOMER-A-200
        """
        vpls_service_id = str(int(vprn_name.split("-")[-1]) * 10)
        base_service_name = "-".join(vprn_name.split("-")[:-1])
        return base_service_name + "-" + vpls_service_id

    def _get_service_id_from_vprn_name(self, vprn_name: str) -> int:
        """
        Extract EVI/service ID from VPLS name suffix.
        Example: INB-MGMT-1200 -> 1200
        """
        return int(vprn_name.split("-")[-1])
