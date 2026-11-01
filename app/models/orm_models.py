"""SQLAlchemy ORM models for the Firestarter topology and resource database."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime


# ============================================================
# STATUS ENUMS
# ============================================================
class IPStatus(StrEnum):
    """
    Lifecycle states for Prefix and IPAddress rows.

    - ``available`` — free, eligible for allocation
    - ``reserved`` — held for a planned topology change, not yet on the network
    - ``allocated`` — committed and actively assigned to a topology element
    - ``retired`` — was once allocated; must never be reused

    The ``retired`` terminal state exists because reusing a P2P address after
    a topology change can confuse neighbour adjacencies and operator memory.
    Once a link is migrated away, its addresses are retired permanently.
    """

    available = "available"
    reserved = "reserved"
    allocated = "allocated"
    retired = "retired"


class CableStatus(StrEnum):
    """
    Deployment lifecycle of a Cable row.

    Allowed transitions:
        planned → connected → active → retired

    - ``planned``   — exists in the DB but not yet wired to interface records
                      (only valid transiently in rolled-back savepoints used
                      by migration discovery; never persisted in normal flow)
    - ``connected`` — wired to two Interface records by a topology builder;
                      not yet confirmed on the physical network
    - ``active``    — confirmed on the network (dashboard activation or
                      migration commit)
    - ``retired``   — permanently decommissioned; row kept for audit.
                      Retired cables must never be selected for new connections.

    ``Cable.in_use`` has been removed; use ``status in (connected, active)``
    to test whether a cable is wired to interfaces.
    """

    planned = "planned"
    connected = "connected"
    active = "active"
    retired = "retired"


class DeviceStatus(StrEnum):
    """
    Lifecycle state of a Device row.

    Lifecycle (happy path): ``planned → preactivated → reachable → active``.

    Allowed transitions (condition-driven, not restricted by prior state):
        any          → unassigned   (device has zero in_use interfaces after all
                                     detaches; set automatically regardless of
                                     prior status)
        unassigned   → planned      (topology builder rewires the device)
        planned      → preactivated (operator declares the location live on
                                     install day)
        planned      → reachable    (admin password set and proven on the device)
        preactivated → reachable    (same, after preactivation)
        reachable    → active       (operator acknowledges — the ONLY path to
                                     active; proven login is the gate, so never
                                     planned/preactivated → active directly)
        planned      → retired      (operator decommission)
        active       → retired      (operator decommission, terminal)

    The operator pages that drove these transitions are not part of this demo;
    the states remain because the pipeline and the rendered configuration depend
    on them.

    - ``planned``      — device exists in the topology model but is not yet
                         confirmed on the network; receives full service
                         computation so configs are ready before going live
    - ``preactivated`` — operator has declared the location live (install day).
                         Generated config ships fully open, so its links come up
                         in ISIS immediately. NOT yet loginable — the production
                         password has not been proven — so it is deliberately
                         excluded from the management (SSH) inventory.
    - ``reachable``    — the factory admin password has been replaced with the
                         production one and the new credential proved by logging
                         in with it. The device answers to us, but is not yet
                         carrying service; activation is still a separate,
                         operator-driven step.
    - ``active``       — device is confirmed operational on the network
    - ``unassigned`` — all cables have been detached; device hardware still
                       exists but has no topology
                       resources. Excluded from service computation.
                       Must have all interfaces freed before reinsertion.
    - ``retired``    — permanently decommissioned. Terminal — must never be
                       re-activated or have new resources allocated to it.
                       Excluded from service computation.
    """

    planned = "planned"
    preactivated = "preactivated"
    reachable = "reachable"
    active = "active"
    unassigned = "unassigned"
    retired = "retired"


# ============================================================
# ROLE & SITE MODELS
# ============================================================
class Role(Base):
    """Device role (e.g. PE, P, CE). Shared reference table used by Device."""

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    devices: Mapped[list[Device]] = relationship(
        "Device",
        back_populates="role",
        cascade="all, delete-orphan",
    )


class Site(Base):
    """Physical or logical site that groups devices and holds an optional postal address."""

    __tablename__ = "site"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    devices: Mapped[list[Device]] = relationship(
        "Device",
        back_populates="site",
        cascade="all, delete-orphan",
    )

    address_id: Mapped[int | None] = mapped_column(
        ForeignKey("address.id", ondelete="SET NULL"), nullable=True
    )
    address: Mapped[Address | None] = relationship("Address", back_populates="site")


# ============================================================
# DEVICE & INTERFACE MODELS
# ============================================================
class Device(Base):
    """Network device node. Carries role, site, model, and a freeform labels dict for selector matching."""

    __tablename__ = "device"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[DeviceStatus] = mapped_column(
        SAEnum(DeviceStatus, name="device_status", native_enum=False),
        default=DeviceStatus.planned,
        nullable=False,
    )
    lag_name: Mapped[str] = mapped_column(String(32), default="")  # Ex: Bundle-Ether
    model_name: Mapped[str] = mapped_column(String(32), default="")
    labels: Mapped[dict[str, str]] = mapped_column(
        MutableDict.as_mutable(JSON),
        default=dict,
        nullable=False,
    )

    role_id: Mapped[int] = mapped_column(
        ForeignKey("role.id", name="fk_device_role_id"), nullable=True, index=True
    )
    role: Mapped[Role] = relationship(
        "Role",
        back_populates="devices",
    )

    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.id", name="fk_device_site_id"), nullable=True, index=True
    )
    site: Mapped[Site] = relationship(
        "Site",
        back_populates="devices",
    )

    interfaces: Mapped[list[Interface]] = relationship(
        "Interface",
        back_populates="device",
        cascade="all, delete-orphan",
        order_by="Interface.name",
    )


class Interface(Base):
    """
    Physical or logical interface on a Device.

    - Supports LAG hierarchy via self-referential parent/children relationship.
    - ``in_use`` marks whether the interface has been allocated to a topology element.
    - ``intf_role`` distinguishes NNI, UNI, etc. for template rendering.
    """

    __tablename__ = "interface"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(128))
    in_use: Mapped[bool] = mapped_column(Boolean, default=False)
    # status: Mapped[str] = mapped_column(String(32), default="")
    intf_role: Mapped[str] = mapped_column(String(32), default="")  # NNI/CE/...
    evpn_esi: Mapped[str] = mapped_column(String(32), nullable=True, default=None)
    connector: Mapped[str] = mapped_column(String(32), default="")

    __table_args__ = (
        Index("ix_interface_needs_esi", "evpn_esi", sqlite_where=text("evpn_esi = 'needs esi'")),
        Index(
            "ix_interface_device_used_name",
            "device_id",
            "name",
            sqlite_where=text("in_use = 1"),
        ),
        Index(
            "ix_interface_nni_used",
            "intf_role",
            sqlite_where=text("in_use = 1"),
        ),
    )

    device_id: Mapped[int] = mapped_column(
        ForeignKey("device.id", name="fk_interface_device_id"), nullable=False, index=True
    )
    device: Mapped[Device] = relationship(
        "Device",
        back_populates="interfaces",
    )

    ip_addresses: Mapped[list[IPAddress]] = relationship(
        "IPAddress",
        back_populates="interface",
        # delete-orphan intentionally omitted: soft-delete (status=retired) is
        # used instead of hard-delete for IPAddress rows, so clearing interface_id
        # must not auto-delete the IP row.
        cascade="all",
    )

    cables_as_a: Mapped[list[Cable]] = relationship(
        "Cable",
        back_populates="interface_a",
        foreign_keys="Cable.interface_a_id",
        cascade="all, delete-orphan",
    )

    cables_as_b: Mapped[list[Cable]] = relationship(
        "Cable",
        back_populates="interface_b",
        foreign_keys="Cable.interface_b_id",
        cascade="all, delete-orphan",
    )

    # ------------------------------------------------------------------
    # LAG Support
    # ------------------------------------------------------------------

    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("interface.id"),
        nullable=True,
    )

    parent: Mapped[Interface | None] = relationship(
        back_populates="children",
        remote_side="Interface.id",
        uselist=False,
    )

    children: Mapped[list[Interface]] = relationship(
        back_populates="parent",
        # No cascade: physical (LAG-child) interfaces are hardware that
        # outlives any software LAG-parent it happens to belong to.
        # This lets a LAG parent be deleted without losing its physical members.
        # Device-level deletion still cleans up everything via the
        # Device.interfaces cascade.
    )


# ============================================================
# Cable class
# ============================================================
class Cable(Base):
    """
    Physical cable connecting two Interface endpoints (interface_a ↔ interface_b).

    The schema deliberately permits multiple Cable rows referencing the same
    interface so that a planned replacement cable can be staged alongside the
    active cable it will retire. Distinguishing them in queries is the caller's
    responsibility (filter by ``status``).
    """

    __tablename__ = "cable"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    interface_a_id: Mapped[int] = mapped_column(
        ForeignKey("interface.id", name="fk_cable_interface_a"),
        nullable=False,
    )
    interface_a: Mapped[Interface] = relationship(
        "Interface",
        foreign_keys=[interface_a_id],
        back_populates="cables_as_a",
    )

    interface_b_id: Mapped[int] = mapped_column(
        ForeignKey("interface.id", name="fk_cable_interface_b"),
        nullable=False,
    )
    interface_b: Mapped[Interface] = relationship(
        "Interface",
        foreign_keys=[interface_b_id],
        back_populates="cables_as_b",
    )

    description: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[CableStatus] = mapped_column(
        SAEnum(CableStatus, name="cable_status", native_enum=False),
        default=CableStatus.planned,
        nullable=False,
    )
    # Physical length of the cable in whole metres. Defaults to 1 m when unset.
    cable_length: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


# ============================================================
# PREFIX POOL TYPES
# ============================================================
class PrefixPoolType(Base):
    """Category of prefix pool (e.g. loopback, p2p). Acts as a type tag on PrefixPool."""

    __tablename__ = "prefix_pool_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)

    pools: Mapped[list[PrefixPool]] = relationship(
        "PrefixPool",
        back_populates="pool_type",
        cascade="all, delete-orphan",
    )


# ============================================================
# PREFIX POOLS
# ============================================================
class PrefixPool(Base):
    """Named IP prefix pool (supernet) from which Prefix and IPAddress entries are carved."""

    __tablename__ = "prefix_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(128))

    type_id: Mapped[int] = mapped_column(
        ForeignKey("prefix_pool_type.id", name="fk_prefix_pool_type"),
        nullable=False,
    )
    pool_type: Mapped[PrefixPoolType] = relationship(
        "PrefixPoolType",
        back_populates="pools",
    )

    prefixes: Mapped[list[Prefix]] = relationship(
        "Prefix",
        back_populates="pool",
        cascade="all, delete-orphan",
    )

    ip_addresses: Mapped[list[IPAddress]] = relationship(
        "IPAddress",
        back_populates="pool",
        cascade="all, delete-orphan",
    )


# ============================================================
# PREFIXES (/31, /30, ...)
# ============================================================
class Prefix(Base):
    """
    A subnet (e.g. /31, /30) allocated from a PrefixPool and assigned to a P2P link.

    The ``status`` enum replaces the older boolean ``in_use`` so that retired
    addresses (those previously allocated and then released by a topology
    migration) are structurally distinguishable from never-used addresses —
    enforcing the "never reuse a P2P prefix" rule at the model layer.
    """

    __tablename__ = "prefix"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prefix: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[IPStatus] = mapped_column(
        SAEnum(IPStatus, name="ip_status", native_enum=False),
        default=IPStatus.available,
        nullable=False,
    )

    pool_id: Mapped[int] = mapped_column(
        ForeignKey("prefix_pool.id", name="fk_prefix_pool"),
        nullable=False,
    )
    pool: Mapped[PrefixPool] = relationship(
        "PrefixPool",
        back_populates="prefixes",
    )

    ip_addresses: Mapped[list[IPAddress]] = relationship(
        "IPAddress",
        back_populates="prefix",
        cascade="all, delete-orphan",
    )


# ============================================================
# IP ADDRESSES (/32 + /31 hosts)
# ============================================================
class IPAddress(Base):
    """
    Single IP address (/32 or host inside a /31) drawn from a PrefixPool and bound to an Interface.

    Shares the ``IPStatus`` enum with Prefix. Only link-role addresses ever
    transition to ``retired`` — loopback/mgmt addresses are device-level
    reservations that a topology change must not touch.
    """

    __tablename__ = "ip_address"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[IPStatus] = mapped_column(
        SAEnum(IPStatus, name="ip_status", native_enum=False),
        default=IPStatus.available,
        nullable=False,
    )
    role: Mapped[str | None] = mapped_column(String(32))  # loopback/link/mgmt

    pool_id: Mapped[int] = mapped_column(
        ForeignKey("prefix_pool.id", name="fk_ipaddress_pool"),
        nullable=False,
    )
    pool: Mapped[PrefixPool] = relationship(
        "PrefixPool",
        back_populates="ip_addresses",
    )

    prefix_id: Mapped[int | None] = mapped_column(
        ForeignKey("prefix.id", name="fk_ipaddress_prefix"),
        nullable=True,
    )
    prefix: Mapped[Prefix | None] = relationship(
        "Prefix",
        back_populates="ip_addresses",
    )

    interface_id: Mapped[int | None] = mapped_column(
        ForeignKey("interface.id", name="fk_ipaddress_interface"),
        nullable=True,
    )
    interface: Mapped[Interface | None] = relationship(
        "Interface",
        back_populates="ip_addresses",
    )


# ============================================================
# ADDRESSES
# ============================================================
class Address(Base):
    """Postal address optionally associated with a Site."""

    __tablename__ = "address"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    street: Mapped[str | None] = mapped_column(String(128), nullable=True)
    city: Mapped[str | None] = mapped_column(String(128), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)

    site: Mapped[Site] = relationship("Site", back_populates="address", uselist=False)


# ============================================================
# Job class
# ============================================================
class Job(Base):
    """
    Pending or executed topology-change job.

    Stores an ``actions_blob`` (list of action dicts) and tracks lifecycle
    state: pending → executed (dry-run OK) → committed (applied to DB).
    """

    __tablename__ = "job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    # JSON blob containing full job description:
    # {
    #    "actions": [
    #        {"action": "add_device", "params": {...}},
    #        {"action": "add_cable", "params": {...}},
    #        ...
    #    ]
    # }

    actions_blob: Mapped[list[dict]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )

    # job state: pending → executed (dry-run OK) → committed (applied to DB)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=True)

    # Timezone-aware UTC end-to-end via UTCDateTime (see app/db/types.py).
    # `default` uses a lambda (not bare datetime.now) because SQLAlchemy passes
    # a context argument to bare callables, which datetime.now would interpret
    # as a `tz` positional — wrong type, immediate error.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), nullable=True
    )
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    results_blob: Mapped[dict | None] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:
        return f"<Job id={self.id} name={self.name} status={self.status}>"


# ============================================================
# User and UserRole classes
# ============================================================


class UserRole(Base):
    """Web UI user role (admin, ro, rw to start). Governs dashboard permissions.

    Roles are a reference table (mirroring the device ``Role`` pattern) rather
    than a Python enum so new roles can be added without a schema change. This
    sprint only *stores and displays* the role; per-action enforcement keyed on
    the role name is a later sprint.
    """

    __tablename__ = "user_role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    users: Mapped[list[User]] = relationship(
        "User", back_populates="role", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<UserRole id={self.id} name={self.name}>"


class User(Base):
    """Dashboard user with an assigned UserRole and a hashed password.

    Authentication is password-based: only the salted hash is persisted (never
    the plaintext) via ``werkzeug.security`` (scrypt by default). ``must_change_password``
    forces a password change on first login — new accounts are created with a
    temporary password and this flag set, so the operator who created the account
    never learns the user's permanent password.
    """

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Timezone-aware UTC end-to-end via UTCDateTime (see app/db/types.py).
    # `default` uses a lambda (not bare datetime.now) so SQLAlchemy does not
    # pass its context argument into datetime.now — same pattern as Job.
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    role_id: Mapped[int] = mapped_column(
        ForeignKey("user_role.id", ondelete="RESTRICT"),
        nullable=False,
    )
    role: Mapped[UserRole] = relationship("UserRole", back_populates="users")

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username} role_id={self.role_id}>"


class DeviceCredential(Base):
    """Hashed reference copy of a shared credential used against network devices.

    Deliberately **not** a row in ``user``. Everything in that table is a
    dashboard login, so putting the device password there would mean anyone
    holding it could authenticate to the dashboard as ``device_admin`` — a
    privilege path nobody intends, and one no role configuration makes safe. A
    separate table cannot be reached by ``authenticate()`` at all.

    Only the hash is stored, so this can *verify* a password an operator typed
    but never *recover* one. That is the point: any caller asks for the password
    every time and checks it against this record, so the production password is
    never at rest anywhere in the system.
    """

    __tablename__ = "device_credential"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Timezone-aware UTC end-to-end via UTCDateTime; lambda default for the same
    # reason as User.created_at — it keeps SQLAlchemy's context argument out of
    # datetime.now().
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), nullable=False
    )

    def __repr__(self) -> str:
        return f"<DeviceCredential name={self.name}>"


class ServiceInstance(Base):
    """
    Computed service instance produced by feature handlers.

    Uniquely identified by (svc_name, tenant, variant). The ``computed``
    JSON blob is the per-device render context written by feature handlers
    and consumed by the Printer.
    """

    __tablename__ = "service_instance"
    __table_args__ = (
        UniqueConstraint(
            "svc_name", "tenant", "variant", name="uq_service_instance_svc_tenant_variant"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    svc_name: Mapped[str] = mapped_column(String, nullable=False)
    tenant: Mapped[str] = mapped_column(String, nullable=False)
    variant: Mapped[str] = mapped_column(String, nullable=False)
    # type: Mapped[str] = mapped_column(String, nullable=False)
    computed: Mapped[dict] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class DelegatedPrefix(Base):
    """
    Idempotency latch for delegated IP prefix allocations (Pattern A — prefix/IP).

    One row per service instance. When in_use=True the reservations dict
    ({"prefix": "10.0.0.0/28"}) is returned on re-runs without re-allocating.
    """

    __tablename__ = "delegated_prefix"

    id: Mapped[int] = mapped_column(primary_key=True)

    name: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
    )

    in_use: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    reservations: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
    )


class IntegerResourcePool(Base):
    """Integer resource pool (VLANs, VNIs, RDs, SIDs, etc.) with a contiguous numeric range."""

    __tablename__ = "integer_resource_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    range_end: Mapped[int] = mapped_column(Integer, nullable=False)

    allocation_strategy: Mapped[str] = mapped_column(String, default="sequential")

    integer_allocations: Mapped[list[IntegerAllocation]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )


class IntegerAllocation(Base):
    """
    Tracks a single integer value reserved from an IntegerResourcePool
    for a named service-instance allocation key (Pattern B — integer).

    Replaces the former two-table design (Allocation + ResourceAllocation):
    - Pool exhaustion check: {r.value for r in pool.integer_allocations}
    - Idempotency check: WHERE allocation_name=? returns existing rows

    UNIQUE(pool_id, value) guarantees no pool slot is double-booked at the DB level.
    UNIQUE(allocation_name, pool_key) is the per-key idempotency guard.
    """

    __tablename__ = "integer_allocation"
    __table_args__ = (
        UniqueConstraint("pool_id", "value", name="uq_integer_allocation_pool_value"),
        UniqueConstraint("allocation_name", "pool_key", name="uq_integer_allocation_name_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    allocation_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    pool_key: Mapped[str] = mapped_column(String(64), nullable=False)
    pool_id: Mapped[int] = mapped_column(ForeignKey("integer_resource_pool.id"), nullable=False)
    value: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    pool: Mapped[IntegerResourcePool] = relationship(back_populates="integer_allocations")


class CeMgmtAddress(Base):
    """
    Persisted, sticky management IP assigned to a CE from the CE-management
    VPRN's delegated ``/28`` of its PE / PE pair.

    One row per CE. Assignments are **stable**: once a CE has an address it never
    moves, even when other CEs are added or removed (see
    ``ce_mgmt_allocator``). The address is drawn from the ``DelegatedPrefix``
    named ``ce_mgmt_{pair_label}`` (``app.domain.ce_mgmt``); hosts ``.1``/``.2``/
    ``.3`` are reserved by convention (VRRP gateway + the two PE IRBs), so CEs
    start at the 4th
    usable host.

    This table is *not* wired into any render template — it exists to back the
    ``report_ce_mgmt`` report and to reserve addresses stably for future config
    use. ``address`` is globally unique (each /28 is disjoint) as a safety net
    against double-booking; ``ce_hostname`` is the per-CE idempotency key.
    """

    __tablename__ = "ce_mgmt_address"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ce_hostname: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    pair_label: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prefix: Mapped[str] = mapped_column(String(64), nullable=False)  # the delegated /28
    address: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
