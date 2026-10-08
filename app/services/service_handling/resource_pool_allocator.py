"""Allocates IP prefixes and integers from pool records with idempotency."""

from __future__ import annotations

import ipaddress

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    DelegatedPrefix,
    IntegerAllocation,
    IntegerResourcePool,
    IPAddress,
    IPStatus,
    Prefix,
    PrefixPool,
)
from app.repositories import (
    get_ips_for_pool,
    get_prefixes_by_pool,
)
from app.utils import require


class ResourcePoolAllocator:
    """
    Allocates IP prefixes, IP addresses, and integer resources from pool records.

    Two allocation patterns are supported:

    Pattern A — IP/Prefix (PrefixPool-based):
      allocate_loopback, allocate_p2p_prefix, allocate_ips_for_p2p,
      allocate_full_p2p, allocate_delegated_prefix_per_service_instance.
      Idempotency for delegated prefixes is tracked via DelegatedPrefix rows.

    Pattern B — Integer (IntegerResourcePool-based):
      allocate_per_service_instance.
      Each allocation produces one IntegerAllocation row per pool key.
      UNIQUE constraints at the DB level prevent double-booking.

    All YAML files are assumed validated upfront by the caller.
    """

    def __init__(self, session: Session):
        self.session = session
        self._cache = {}

    # ----------------------------------------------------------------------
    # /32 ALLOCATOR
    # ----------------------------------------------------------------------
    def allocate_loopback(
        self,
        pool: PrefixPool,
        role: str = "loopback",
    ) -> IPAddress:
        """
        Allocate the next free /32 IP address inside a PrefixPool.

        Assumes the pool.prefix is a parent block (e.g., 10.10.0.0/16).

        Raises
        ------
        RuntimeError
            If no /32 address is available.
        """
        network = ipaddress.ip_network(pool.prefix)
        existing_ips = get_ips_for_pool(self.session, pool)

        used_hosts = {ipaddress.ip_address(ip.address.split("/")[0]) for ip in existing_ips}

        for host in network.hosts():
            if host not in used_hosts:
                ip_obj = IPAddress(
                    address=f"{host}/32",
                    pool_id=pool.id,
                    prefix_id=None,
                    role=role,
                    status=IPStatus.available,
                )
                self.session.add(ip_obj)
                self.session.flush()
                return ip_obj

        raise RuntimeError(f"No free /32 addresses left in pool {pool.name}")

    # ----------------------------------------------------------------------
    # /31 ALLOCATOR
    # ----------------------------------------------------------------------
    def allocate_p2p_prefix(self, pool: PrefixPool) -> Prefix:
        """
        Allocate a free /31 prefix from the given pool.

        Raises
        ------
        RuntimeError
            If no /31 prefix is available.
        """
        parent_network = ipaddress.ip_network(pool.prefix)
        used_prefixes = {
            ipaddress.ip_network(p.prefix) for p in get_prefixes_by_pool(self.session, pool)
        }

        for candidate in parent_network.subnets(new_prefix=31):
            if candidate not in used_prefixes:
                prefix = Prefix(
                    prefix=str(candidate),
                    pool_id=pool.id,
                    status=IPStatus.allocated,
                )
                self.session.add(prefix)
                self.session.flush()
                return prefix

        raise RuntimeError(f"No free /31 prefixes left in pool {pool.name}")

    # ----------------------------------------------------------------------
    # HOST IP ALLOCATION FOR /31
    # ----------------------------------------------------------------------
    def allocate_ips_for_p2p(
        self,
        prefix: Prefix,
        role: str = "link",
    ) -> list[IPAddress]:
        """
        Create two IP addresses inside a /31 Prefix.

        Raises
        ------
        RuntimeError
            If prefix is not a /31 block.
        """
        network = ipaddress.ip_network(prefix.prefix)
        hosts = list(network.hosts())

        if network.prefixlen != 31:
            raise RuntimeError(f"Prefix {prefix.prefix} is not a valid /31 network")

        results: list[IPAddress] = []
        for host in hosts:
            ip_obj = IPAddress(
                address=f"{host}/31",
                pool_id=prefix.pool_id,
                prefix_id=prefix.id,
                role=role,
                status=IPStatus.available,
            )
            self.session.add(ip_obj)
            results.append(ip_obj)

        self.session.flush()
        return results

    # ----------------------------------------------------------------------
    # CONVENIENCE: allocate both prefix and IP pairs
    # ----------------------------------------------------------------------
    def allocate_full_p2p(self, pool: PrefixPool) -> tuple[Prefix, list[IPAddress]]:
        """
        Allocate both the /31 prefix AND its two IP addresses.
        """
        prefix = self.allocate_p2p_prefix(pool)
        ips = self.allocate_ips_for_p2p(prefix)
        return prefix, ips

    def allocate_delegated_prefix_per_service_instance(
        self,
        *,
        allocation_name: str,
        pool: PrefixPool,
        prefixlen: int,
    ) -> dict[str, str]:
        """
        Idempotent delegated-prefix allocation (Pattern A).

        Returns the same {"prefix": ...} dict on every re-run for the same
        allocation_name. Idempotency is tracked via DelegatedPrefix rows.
        """
        dp = self.session.scalars(
            select(DelegatedPrefix).where(DelegatedPrefix.name == allocation_name)
        ).one_or_none()

        if dp and dp.in_use:
            return dp.reservations

        if not dp:
            dp = DelegatedPrefix(
                name=allocation_name,
                in_use=False,
                reservations={},
            )
            self.session.add(dp)
            self.session.flush()

        parent = ipaddress.ip_network(pool.prefix)
        used = {ipaddress.ip_network(p.prefix) for p in get_prefixes_by_pool(self.session, pool)}

        for candidate in parent.subnets(new_prefix=prefixlen):
            if candidate not in used:
                delegated = Prefix(
                    prefix=str(candidate),
                    pool_id=pool.id,
                    status=IPStatus.allocated,
                )
                self.session.add(delegated)
                self.session.flush()
                break
        else:
            raise RuntimeError(f"No free /{prefixlen} in pool {pool.name}")

        dp.reservations = {"prefix": delegated.prefix}
        dp.in_use = True
        self.session.flush()

        return dp.reservations

    # ----------------------------------------------------------------------
    # Integer resource pools (Pattern B)
    # ----------------------------------------------------------------------
    def allocate_per_service_instance(
        self,
        *,
        allocation_name: str,
        allocations: dict[str, str],
    ) -> dict[str, int]:
        """
        Idempotent integer allocation from named IntegerResourcePools (Pattern B).

        Each entry in `allocations` maps a pool_key (logical name, e.g. "vlan")
        to an IntegerResourcePool name.  One IntegerAllocation row is created per
        key.  On re-run, existing rows are returned without touching the pools.

        Example::

            allocate_per_service_instance(
                allocation_name="evpn_svc_a",
                allocations={"vlan": "evpn_vlan_pool", "rd": "evpn_rd_pool"},
            )
        """
        existing_rows = self.session.scalars(
            select(IntegerAllocation).where(IntegerAllocation.allocation_name == allocation_name)
        ).all()

        existing = {r.pool_key: r.value for r in existing_rows}

        if set(existing.keys()) == set(allocations.keys()):
            return existing

        result = dict(existing)
        for pool_key, pool_name in allocations.items():
            if pool_key in existing:
                continue

            pool = require(
                self.session.scalars(
                    select(IntegerResourcePool).where(IntegerResourcePool.name == pool_name)
                ).one_or_none(),
                f"IntegerResourcePool '{pool_name}' not found in DB",
            )

            used = {a.value for a in pool.integer_allocations}
            for candidate in range(pool.range_start, pool.range_end + 1):
                if candidate not in used:
                    value = candidate
                    break
            else:
                raise RuntimeError(f"Pool '{pool_name}' exhausted")

            self.session.add(
                IntegerAllocation(
                    allocation_name=allocation_name,
                    pool_key=pool_key,
                    pool=pool,
                    value=value,
                )
            )
            result[pool_key] = value

        self.session.flush()
        return result
