"""Attaches CE devices to PE UNI ports — single-homed and dual-homed (PE-pair) scenarios."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Device, Interface
from app.repositories import get_device_by_hostname, get_role_by_name, get_site_by_name
from app.services.topology_building.topology_builder import TopologyBuilder
from app.utils import require


class CEAttachmentBuilder:
    """
    Attach a logical (non-modeled) CE to one PE (single-homed)
    or to both PEs of a PE-pair (dual-homed).

    Persistence:
    - Marks PE UNI ports as in_use
    - Creates PE UNI LAG(s) (Bundle-Ether etc.) with globally allocated ID
    - Attaches UNI(s) into the LAG(s)
    - Adds descriptions for traceability

    NOTE: No Cable records are created because CE interfaces are not modeled.
    """

    def __init__(
        self,
        *,
        session: Session,
        topology_builder: TopologyBuilder,
    ) -> None:
        self.session = session
        self.topology_builder = topology_builder

    def attach_ce(
        self,
        *,
        site_name: str,
        ce_name: str,
        pe_role_name: str,
        ce_role_name: str,
        ce_model_name: str,
        pe_pair_label: str | None = None,
        connected_pe: str | None = None,
    ) -> Device:
        # 1) Resolve CE record once (CE is logical, but you keep a Device row for convenience)
        site = require(
            get_site_by_name(self.session, site_name),
            f"No DB record found for site '{site_name}'",
        )
        ce_role = require(
            get_role_by_name(self.session, ce_role_name),
            f"No DB record found for role '{ce_role_name}'",
        )
        ce = self._get_or_create_ce(
            ce_name=ce_name,
            site=site,
            role=ce_role,
            model_name=ce_model_name,
        )

        # 2) Choose PE(s)
        if connected_pe is not None:
            # Used when explicitly one PE is chosen (needed in lab)
            pe = require(
                get_device_by_hostname(session=self.session, hostname=connected_pe),
                f"No DB record found for {connected_pe}",
            )
            chosen_pes = [pe]
        else:
            chosen_pes, _ = self._select_pes_for_attachment(
                pe_role_name=pe_role_name,
                site_name=site_name,
                pe_pair_label=pe_pair_label,
            )
        # Explicit check — require() allows bools through.
        if len(chosen_pes) not in (1, 2):
            raise ValueError("Internal error: invalid PE selection")

        dual_homed = len(chosen_pes) == 2

        # 3) Build PE-side access constructs; verify LAG names are equal across both PEs.
        lag_names: set[str] = set()
        for pe in chosen_pes:
            lag = self._build_access_on_pe(
                pe=pe,
                ce_name=ce_name,
                dual_homed=dual_homed,
            )
            lag_names.add(lag.name)
        # Explicit check — require() allows bools through.
        if len(lag_names) != 1:
            raise ValueError("Internal error: LAG names must be equal")

        # Ensure CE exists in-session with PK assigned if newly created
        self.session.flush()
        return ce

    # ---------------------------------------------------------------------
    # CE helpers
    # ---------------------------------------------------------------------
    def _get_or_create_ce(self, *, ce_name: str, site, role, model_name: str) -> Device:
        """Return an existing CE Device row or create a new one (idempotent)."""
        ce = get_device_by_hostname(self.session, ce_name)
        if ce is not None:
            return ce

        ce = Device(hostname=ce_name, site=site, role=role, model_name=model_name)
        self.session.add(ce)
        self.session.flush()
        return ce

    # ---------------------------------------------------------------------
    # PE-side build helpers
    # ---------------------------------------------------------------------
    def _build_access_on_pe(
        self,
        *,
        pe: Device,
        ce_name: str,
        dual_homed: bool,
    ) -> Interface:
        uni_members: list[Interface] = []

        # First UNI
        uni = self.topology_builder.device_builder.select_free_uni(pe)
        uni.in_use = True
        uni_members.append(uni)

        # Create LAG
        lag = self.topology_builder.device_builder.create_uni_lag(pe)
        lag.in_use = True
        self.topology_builder.device_builder.attach_to_lag(uni, lag)

        # Single-homed CEs use two physical UNI members for local LAG redundancy;
        # dual-homed CEs contribute one port per PE instead.
        if not dual_homed:
            uni = self.topology_builder.device_builder.select_free_uni(pe)
            uni.in_use = True
            uni_members.append(uni)
            self.topology_builder.device_builder.attach_to_lag(uni, lag)

        lag.evpn_esi = "needs esi" if dual_homed else "single-homed attached CE"
        lag.description = f"CE:{ce_name} (access LAG id {lag.name.rsplit('-', 1)[-1]})"

        for member in uni_members:
            member.description = f"UNI to CE:{ce_name} via {lag.name}"

        self.session.flush()
        return lag

    # ---------------------------------------------------------------------
    # Selection logic
    # ---------------------------------------------------------------------
    def _select_pes_for_attachment(
        self,
        *,
        site_name: str,
        pe_pair_label: str | None,
        pe_role_name: str,
    ) -> tuple[list[Device], str | None]:
        """
        Returns:
          - ([pe_a, pe_b], label) for a usable pair
          - ([pe], None) for single-homed fallback
        """
        pe_devices = self._get_pes_on_site(site_name, pe_role_name=pe_role_name)

        # Group PE pairs by label "pe-pair:<site>-N"
        pair_map = self._get_pair_map(site_name, pe_devices)

        # 1) Override: must exist and must have capacity on BOTH
        if pe_pair_label:
            pe_pair = pair_map.get(pe_pair_label)
            if not pe_pair:
                raise RuntimeError(f"PE pair '{pe_pair_label}' not found on site '{site_name}'")
            if len(pe_pair) != 2:
                raise RuntimeError(
                    f"PE pair '{pe_pair_label}' must have exactly two PEs (found {len(pe_pair)})"
                )
            if not (self._has_free_uni(pe_pair[0]) and self._has_free_uni(pe_pair[1])):
                raise RuntimeError(f"PE pair '{pe_pair_label}' has insufficient UNI capacity")
            return pe_pair, pe_pair_label

        # 2) Auto: first usable pair by ascending pair index
        for label, pair in self._sorted_pairs(pair_map):
            if len(pair) != 2:
                continue
            if self._has_free_uni(pair[0]) and self._has_free_uni(pair[1]):
                return pair, label

        # 3) Fallback: single PE with free UNI
        for pe in pe_devices:
            if self._has_free_uni(pe):
                return [pe], None

        raise RuntimeError(f"No PE (or PE pair) with free UNI capacity on site '{site_name}'")

    def _get_pes_on_site(self, site_name: str, pe_role_name: str) -> list[Device]:
        """Return all devices with the given role on the given site; raises if none found."""
        pe_devices = list(
            self.session.execute(
                select(Device)
                .where(Device.site.has(name=site_name))
                .where(Device.role.has(name=pe_role_name))
            )
            .scalars()
            .all()
        )
        require(pe_devices, f"No device with role '{pe_role_name}' found on site '{site_name}'")
        return pe_devices

    @staticmethod
    def _get_pair_map(site_name: str, pe_devices: list[Device]) -> dict[str, list[Device]]:
        """Build a mapping of pair_label → [Device, ...] for PE devices that carry a pair label."""
        prefix = f"pe-pair:{site_name}-"
        pair_map: dict[str, list[Device]] = {}

        for pe in pe_devices:
            labels = pe.labels or {}  # defensive if nullable
            pair = labels.get("pair_label", "")
            if pair.startswith(prefix):
                pair_map.setdefault(pair, []).append(pe)

        return pair_map

    @staticmethod
    def _sorted_pairs(pair_map: dict[str, list[Device]]) -> list[tuple[str, list[Device]]]:
        """
        Sort by numeric suffix of 'pe-pair:<site>-N'. If parsing fails, error.
        """

        def key(lbl: str) -> tuple[int, str]:
            try:
                n = int(lbl.rsplit("-", 1)[-1])
                return (n, lbl)
            except Exception as err:
                raise RuntimeError(f"Invalid PE pair label format: {lbl}") from err

        return sorted(pair_map.items(), key=lambda item: key(item[0]))

    def _has_free_uni(self, device: Device) -> bool:
        """Return True if the device has at least one unallocated UNI port."""
        try:
            self.topology_builder.device_builder.select_free_uni(device)
            return True
        except RuntimeError:
            return False
