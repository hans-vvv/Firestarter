from __future__ import annotations

import pytest

from app.domain.file_locations import FileLocations
from app.models.dataclass_models import DeviceSelectorView, RoleView
from app.services.selectors.selector_engine import SelectorEngine
from app.services.service_handling.addressing_policy_resolver import (
    AddressingPolicyResolver,
)


@pytest.fixture
def selector_engine(session):
    return SelectorEngine(session=session)


@pytest.fixture
def resolver(selector_engine):
    resolver = AddressingPolicyResolver(selector_engine=selector_engine)
    resolver.install()
    return resolver


@pytest.fixture
def make_device_view():
    def _make(
        *,
        hostname: str,
        role: str,
        tenant: str = "lab",
    ) -> DeviceSelectorView:
        return DeviceSelectorView(
            hostname=hostname,
            labels={"tenant": tenant},
            role=RoleView(name=role),
        )

    return _make


@pytest.mark.parametrize(
    ("role", "expected_pool"),
    [
        ("core", "core_loopback0_pool_lab"),
        ("pe", "pe_loopback0_pool_lab"),
    ],
)
def test_resolve_loopback0_pool_by_role(
    resolver,
    make_device_view,
    role,
    expected_pool,
):
    view = make_device_view(
        hostname=f"{role}1.tst-001",
        role=role,
    )

    pool = resolver.resolve_loopback0_pool(view)
    assert pool == expected_pool


@pytest.mark.parametrize(
    ("role", "expected_pool"),
    [
        ("core", "core_loopback0_pool_lab"),
        ("pe", "pe_loopback0_pool_lab"),
    ],
)
def test_resolve_loopback0_pool_by_role(
    resolver,
    make_device_view,
    role,
    expected_pool,
):
    view = make_device_view(
        hostname=f"{role}1.tst-001",
        role=role,
    )

    pool = resolver.resolve_loopback0_pool(view)
    assert pool == expected_pool


def test_resolve_loopback0_no_matching_policy_fails(
    resolver,
    make_device_view,
):
    view = make_device_view(
        hostname="core1.tst-001",
        role="core",
        tenant="unknown_tenant",
    )

    with pytest.raises(ValueError, match="No addressing policy matches device"):
        resolver.resolve_loopback0_pool(view)


def test_resolve_loopback0_missing_role_mapping_fails(
    resolver,
    make_device_view,
):
    view = make_device_view(
        hostname="core1.tst-001",
        role="test-switch",
    )

    with pytest.raises(ValueError, match="No loopback0 pool defined for role"):
        resolver.resolve_loopback0_pool(view)


@pytest.mark.parametrize(
    ("role_a", "role_b", "expected_pool"),
    [
        ("core", "core", "p2p_pool_lab"),
        ("core", "pe", "p2p_pool_lab"),
        ("pe", "pe", "p2p_pool_lab"),
    ],
)
def test_resolve_p2p_pool_by_role_pair(
    resolver,
    make_device_view,
    role_a,
    role_b,
    expected_pool,
):
    dev_a = make_device_view(
        hostname="core1.tst-001",
        role=role_a,
    )
    dev_b = make_device_view(
        hostname="core1.tst-001",
        role=role_b,
    )

    pool = resolver.resolve_p2p_pool(dev_a, dev_b)
    assert pool == expected_pool


def test_resolve_p2p_no_matching_policy_fails(
    resolver,
    make_device_view,
):
    dev_a = make_device_view(
        hostname="core1.tst-001",
        role="core",
        tenant="lab",
    )
    dev_b = make_device_view(
        hostname="core2.tst-001",
        role="core",
        tenant="other",
    )

    with pytest.raises(RuntimeError, match="No addressing policy matched devices"):
        resolver.resolve_p2p_pool(dev_a, dev_b)


def test_resolve_p2p_missing_role_pair_fails(
    resolver,
    make_device_view,
):
    dev_a = make_device_view(
        hostname="rr1.tst-001",
        role="rr",
    )
    dev_b = make_device_view(
        hostname="rr2.tst-001",
        role="rr",
    )

    with pytest.raises(RuntimeError, match="no p2p pool defined for role pair"):
        resolver.resolve_p2p_pool(dev_a, dev_b)


def test_install_requires_at_least_one_policy(
    selector_engine,
    tmp_path,
    monkeypatch,
):
    empty_dir = tmp_path / "addr_defs"
    empty_dir.mkdir()

    import app.services.service_handling.addressing_policy_resolver as resolver_mod

    monkeypatch.setattr(
        resolver_mod,
        "ADDRESSING_DEF_LOC",
        FileLocations(location=str(empty_dir)),
    )

    resolver = AddressingPolicyResolver(selector_engine=selector_engine)

    with pytest.raises(ValueError, match="No addressing policies found"):
        resolver.install()


# ---------------------------------------------------------------------------
# resolve_loopback1_pool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected_pool"),
    [
        ("core", "core_loopback1_pool_lab"),
        ("pe", "pe_loopback1_pool_lab"),
    ],
)
def test_resolve_loopback1_pool_by_role(resolver, make_device_view, role, expected_pool):
    view = make_device_view(hostname=f"{role}1.tst-001", role=role)
    pool = resolver.resolve_loopback1_pool(view)
    assert pool == expected_pool


def test_resolve_loopback1_missing_role_mapping_fails(resolver, make_device_view):
    view = make_device_view(hostname="core1.tst-001", role="test-switch")
    with pytest.raises(ValueError, match="No loopback1 pool defined for role"):
        resolver.resolve_loopback1_pool(view)


# ---------------------------------------------------------------------------
# _resolve_device_policy — multiple matching policies
# ---------------------------------------------------------------------------


def test_resolve_device_policy_multiple_matches_raises(resolver, make_device_view):
    """Injecting a duplicate policy causes the multiple-match guard to fire."""
    view = make_device_view(hostname="core1.tst-001", role="core")
    # Duplicate the first loaded policy so both match
    duplicate = dict(resolver._policies[0])
    resolver._policies.append(duplicate)
    with pytest.raises(ValueError, match="Multiple addressing policies match device"):
        resolver.resolve_loopback0_pool(view)


# ---------------------------------------------------------------------------
# resolve_p2p_pool — multiple matching policies
# ---------------------------------------------------------------------------


def test_resolve_p2p_multiple_policies_raises(resolver, make_device_view):
    """Injecting a duplicate policy triggers the multiple-match guard for p2p."""
    dev_a = make_device_view(hostname="core1.tst-001", role="core")
    dev_b = make_device_view(hostname="core2.tst-001", role="core")
    duplicate = dict(resolver._policies[0])
    resolver._policies.append(duplicate)
    with pytest.raises(RuntimeError, match="Multiple addressing policies matched devices"):
        resolver.resolve_p2p_pool(dev_a, dev_b)
