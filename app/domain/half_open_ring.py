"""Shared topology facts about half-open PE rings.

A ``HalfOpenRings`` sheet row models one ring terminated by two core routers,
with a chain of PE devices between them::

    core_a  ── PE chain ──  core_b

The two termination *sites* are named in the ``Termination_site_a`` /
``Termination_site_b`` columns; the terminating core routers are **derived**
from those site names (``core1.<site>``), never spelled out in the sheet. Every
consumer that needs the ring's end members — today the ingestion cabler in
``excel_data_handler``, which emits the two end cables ``core_a → first PE``
and ``last PE → core_b`` — calls here, so the derivation cannot drift between
callers: if two places disagreed, a correct sheet would either mis-cable or
fail validation.
"""

from __future__ import annotations


def terminating_devices(*, site_a: str, site_b: str) -> tuple[str, str]:
    """Return ``(core_a, core_b)`` hostnames terminating a half-open ring.

    The default is ``core1.<site>`` at each end. When both termination sites
    are the same, the far end is the *second* core router on that site
    (``core2.<site>``) so the ring still closes on two distinct devices.
    """
    core_a = f"core1.{site_a}"
    core_b = f"core2.{site_b}" if site_a == site_b else f"core1.{site_b}"
    return core_a, core_b
