from __future__ import annotations

"""Tests for the half-open ring termination derivation.

The ring's end members are never written in the ``HalfOpenRings`` sheet: they
are derived from the two termination site names. These tests pin the rule so a
change here is a deliberate one — the ingestion cabler relies on it to emit the
two end cables.
"""

from app.domain.half_open_ring import terminating_devices


def test_distinct_sites_terminate_on_core1_of_each_site():
    assert terminating_devices(site_a="ams-001", site_b="rtm-001") == (
        "core1.ams-001",
        "core1.rtm-001",
    )


def test_same_site_terminates_on_core1_and_core2():
    assert terminating_devices(site_a="tst-001", site_b="tst-001") == (
        "core1.tst-001",
        "core2.tst-001",
    )


def test_is_order_sensitive():
    """``site_a`` is always the head of the ring, ``site_b`` the tail."""
    a = terminating_devices(site_a="x", site_b="y")
    b = terminating_devices(site_a="y", site_b="x")
    assert a == tuple(reversed(b))
