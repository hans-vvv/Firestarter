"""Tests for the Nokia SR OS MD-CLI driver.

The overridden methods are the ones that talk to a device, so each test drives
them against a stub that records what was written to the channel and replays
canned reads. No network, no netmiko connection setup.
"""

from __future__ import annotations

import re

import pytest
from netmiko.nokia.nokia_sros import NokiaSrosSSH
from netmiko.ssh_dispatcher import CLASS_MAPPER, platforms

from app.automation.driver import DEVICE_TYPE, NokiaSrosMdCliSSH, register


class FakeChannel(NokiaSrosMdCliSSH):
    """Driver instance with the transport replaced by recordable stubs.

    Subclasses rather than mocks so the real method bodies run — the point is to
    test our overrides, not a mock of them.
    """

    def __init__(self, *, base_prompt: str = "admin@tst-001", reads: list[str] | None = None):
        # Deliberately skip BaseConnection.__init__: it opens a connection.
        self.base_prompt = base_prompt
        self.global_cmd_verify = False
        self.written: list[str] = []
        self._reads = list(reads or [])
        self.RETURN = "\n"

    def write_channel(self, out_data: str) -> None:
        self.written.append(out_data)

    def normalize_cmd(self, command: str) -> str:
        return command.rstrip() + "\n"

    def read_until_pattern(self, pattern: str = "", **kwargs) -> str:
        self.last_pattern = pattern
        return self._reads.pop(0) if self._reads else ""


def _stage_config_mode_checks(dev, values):
    """Script the two check_config_mode() calls config_mode makes: before, after."""
    seq = iter(values)
    dev.check_config_mode = lambda *a, **k: next(seq)


class TestConfigMode:
    def test_uses_configure_exclusive_not_edit_config(self):
        """Upstream sends 'edit-config exclusive'; our devices want 'configure'."""
        assert NokiaSrosMdCliSSH.config_mode.__defaults__[0] == "configure exclusive"
        assert NokiaSrosSSH.config_mode.__defaults__[0] == "edit-config exclusive"

    def test_default_pattern_matches_prompt_without_ex_marker(self):
        """The inherited pattern requires an '(ex)' marker our prompt lacks.

        Asserted against the pattern the method actually reads with. The
        override no longer delegates to NokiaSrosSSH.config_mode — it
        reimplements it so a failure can report the device's own response —
        so there is no delegation call left to intercept.
        """
        dev = FakeChannel(base_prompt="admin@tst-001", reads=["[ex:/configure]\nadmin@tst-001#"])
        _stage_config_mode_checks(dev, [False])

        dev.config_mode()

        assert re.search(dev.last_pattern, "admin@tst-001# ")
        assert "(ex)" not in dev.last_pattern
        assert dev.written == ["configure exclusive\n"]

    def test_explicit_pattern_is_respected(self):
        # Output must carry the config-mode marker: success is now judged from
        # the transition output rather than a follow-up probe.
        dev = FakeChannel(reads=["[ex:/configure]\nadmin@tst-001#"])
        _stage_config_mode_checks(dev, [False])

        dev.config_mode(pattern=r"custom")

        assert dev.last_pattern == r"custom"


class TestCheckConfigMode:
    def test_classic_cli_is_never_in_config_mode(self):
        """No '@' in the prompt means classic CLI, which has no config mode."""
        dev = FakeChannel(base_prompt="A:tst-001")
        assert NokiaSrosMdCliSSH.check_config_mode(dev) is False

    def test_defaults_match_our_prompt_marker_as_regex(self):
        defaults = NokiaSrosMdCliSSH.check_config_mode.__defaults__
        check_string, _pattern, force_regex = defaults
        assert check_string == r"\[ex:"
        assert force_regex is True
        # The literal upstream default would not match our prompt.
        assert NokiaSrosSSH.check_config_mode.__defaults__[0] == r"(ex)["

    def test_check_string_matches_a_real_md_cli_prompt(self):
        assert re.search(r"\[ex:", "(ex)[configure][ex:/configure] admin@tst-001# ")


class TestExitAll:
    def test_reads_until_our_prompt_terminator(self):
        dev = FakeChannel(base_prompt="admin@tst-001", reads=["admin@tst-001# "])
        out = dev._exit_all()

        assert dev.written == ["exit all\n"]
        assert out == "admin@tst-001# "
        # The pattern must be anchored on the escaped prompt, not read_until_prompt.
        assert re.escape("admin@tst-001") in dev.last_pattern

    def test_reads_command_echo_first_when_verifying(self):
        """With cmd verification on, the echo must be consumed before the prompt."""
        dev = FakeChannel(base_prompt="admin@tst-001", reads=["exit all", "admin@tst-001# "])
        dev.global_cmd_verify = True
        out = dev._exit_all()
        assert out == "exit all" + "admin@tst-001# "

    def test_prompt_regex_is_escaped(self):
        """Prompts contain regex metacharacters; they must not be interpreted."""
        dev = FakeChannel(base_prompt="admin@tst-001[a]", reads=["admin@tst-001[a]# "])
        dev._exit_all()
        assert r"\[a\]" in dev.last_pattern


class TestRegistration:
    def test_register_adds_device_type(self):
        register()
        assert CLASS_MAPPER[DEVICE_TYPE] is NokiaSrosMdCliSSH
        assert DEVICE_TYPE in platforms

    def test_register_is_idempotent(self):
        register()
        register()
        assert platforms.count(DEVICE_TYPE) == 1

    def test_stock_nokia_sros_is_left_alone(self):
        """We add a device type; we must not redefine netmiko's own."""
        register()
        assert CLASS_MAPPER["nokia_sros"] is NokiaSrosSSH
