from __future__ import annotations

"""Tests for the IS-IS ``overload`` underlay line and its role gating.

The line ``/configure router "Base" isis 0 overload`` is emitted only when the
instance context carries ``set_overload_bit`` truthy. That flag is computed by
``ISISCoreFeatureHandler`` from ``set_overload_bit_for_roles`` in the service
definition (a device's role must appear in that list). Here we cover the
template half: that ``underlay.j2`` renders the line iff the flag is set.

The render drives ``underlay.j2`` through the Printer's own Jinja environment,
so the template is wired exactly as in a real render.
"""

from pathlib import Path

import pytest

from app.printing.printer import Printer

TEMPLATE_DIR = Path("app/services/templates/sros")
OVERLOAD_LINE = '/configure router "Base" isis 0 overload'


def _render_underlay(session, *, set_overload_bit):
    """Render underlay.j2 for a device with an IS-IS instance-0 context."""
    env = Printer(session=session)._get_env(TEMPLATE_DIR)
    template = env.get_template("underlay.j2")
    return template.render(
        hostname="rr1.tst-001",
        device_role_name="rr",
        tenant="lab",
        interfaces=[],
        isis={
            "instances": {
                "0": {
                    "instance_id": 0,
                    "area_address": "31.0001",
                    "keychain_name": "ISIS-KEYCHAIN",
                    "metric_style": "wide",
                    "set_overload_bit": set_overload_bit,
                    "passive_interfaces": ["system"],
                    "interfaces": [],
                }
            }
        },
    )


class TestOverloadLineGating:
    def test_line_present_when_flag_set(self, session):
        config = _render_underlay(session, set_overload_bit=True)
        assert OVERLOAD_LINE in config

    def test_line_absent_when_flag_unset(self, session):
        config = _render_underlay(session, set_overload_bit=False)
        assert OVERLOAD_LINE not in config

    def test_line_absent_when_flag_missing(self, session):
        """A stray instance ctx without the key must not blow up (StrictUndefined)
        and must not emit the line."""
        env = Printer(session=session)._get_env(TEMPLATE_DIR)
        template = env.get_template("underlay.j2")
        config = template.render(
            hostname="core1.tst-001",
            device_role_name="core",
            tenant="lab",
            interfaces=[],
            isis={
                "instances": {
                    "0": {
                        "instance_id": 0,
                        "area_address": "31.0001",
                        "keychain_name": "ISIS-KEYCHAIN",
                        "metric_style": "wide",
                        "passive_interfaces": [],
                        "interfaces": [],
                    }
                }
            },
        )
        assert OVERLOAD_LINE not in config

    def test_flag_does_not_disturb_the_rest_of_the_isis_block(self, session):
        config = _render_underlay(session, set_overload_bit=True)
        assert '/configure router "Base" isis 0 admin-state enable' in config
        assert '/configure router "Base" isis 0 area-address [31.0001]' in config
        assert '/configure router "Base" isis 0 interface "system" passive true' in config
