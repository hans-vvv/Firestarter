"""Creates a PE pair: two PE devices wired together with P2P cables and IPs."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Cable, Device, PrefixPool

from .device_builder import DeviceBuilder
from .topology_builder import TopologyBuilder


class PEPairBuilder:
    """
    Coordinates the creation and connection of PE device pairs.
    The two PE Devices must have been created upfront.

    Responsibilities:
    - Connect them with a point-to-point link.
    - Create identical label on both PEs so CEs can be dual-homed
    """

    def __init__(
        self,
        *,
        session: Session,
        topology_builder: TopologyBuilder,
        device_builder: DeviceBuilder,
    ) -> None:
        self.session = session
        self.topology_builder = topology_builder
        self.device_builder = device_builder

    # --------------------------------------------------------------
    def create_pe_pair(
        self,
        *,
        on_lag: bool,
        p2p_pool: PrefixPool,
        dev_a: Device,
        dev_b: Device,
    ) -> Cable:
        """
        Create two PE devices as a pair with connecting cable/p2p link
        and with a shared label to connect dual-homed CEs
        """

        site_a_name = dev_a.site.name
        site_b_name = dev_b.site.name

        if site_a_name != site_b_name:
            raise ValueError("Both devices must share the same Site")

        # Assign pair label to both devices
        #  TODO: create logic to connect CE to 2nd PE pair on site
        dev_a.labels["pair_label"] = f"pe-pair:{site_a_name}-1"
        dev_b.labels["pair_label"] = f"pe-pair:{site_a_name}-1"

        # Connect them with a P2P link (includes IP + cable)
        cable = self.topology_builder.build_p2p_link(
            dev_a_name=dev_a.hostname, dev_b_name=dev_b.hostname, pool=p2p_pool, on_lag=on_lag
        )

        self.session.flush()
        return cable
