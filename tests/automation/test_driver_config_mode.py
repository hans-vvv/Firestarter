"""Tests for the driver's ``config_mode`` override.

The override exists to report *why* entry failed. netmiko reads the device's
response and then raises a fixed string, discarding it — which makes an ordinary
condition (another session holding the exclusive lock) indistinguishable from a
genuine fault. That cost a debugging round-trip during lab testing, hence these.

Driven through a scripted fake rather than a device.
"""

from __future__ import annotations

import pytest

from app.automation.driver import NokiaSrosMdCliSSH


class FakeChannel(NokiaSrosMdCliSSH):
    """A driver instance with the transport replaced by scripted reads."""

    def __init__(self, *, reads, in_config_mode, base_prompt="A:admin@core1.tst-001"):
        # Deliberately no super().__init__() — that would open an SSH session.
        self.base_prompt = base_prompt
        self.global_cmd_verify = False
        self._reads = list(reads)
        self._in_config_mode = list(in_config_mode)
        self.written: list[str] = []

    def normalize_cmd(self, cmd):
        return cmd

    def write_channel(self, out):
        self.written.append(out)

    def read_until_pattern(self, pattern="", re_flags=0):
        return self._reads.pop(0)

    def check_config_mode(self, *args, **kwargs):
        return self._in_config_mode.pop(0)


ENTERED = "[ex:/configure]\nA:admin@core1.tst-001#"


class TestConfigMode:
    def test_returns_early_when_already_in_config_mode(self):
        conn = FakeChannel(reads=[], in_config_mode=[True])

        assert conn.config_mode() == ""
        assert conn.written == []

    def test_success_is_judged_from_the_transition_output(self):
        """Not from a second probe — SR OS prints the marker only once.

        check_config_mode is deliberately made to lie here: if the method still
        consulted it, this would raise.
        """
        conn = FakeChannel(reads=[ENTERED], in_config_mode=[False, False])

        assert "[ex:/configure]" in conn.config_mode()

    def test_sends_configure_exclusive(self):
        conn = FakeChannel(reads=[ENTERED], in_config_mode=[False])

        conn.config_mode()

        assert conn.written == ["configure exclusive"]

    def test_returns_the_transition_output(self):
        conn = FakeChannel(reads=[ENTERED], in_config_mode=[False])

        assert "[ex:/configure]" in conn.config_mode()

    def test_failure_reports_what_the_device_said(self):
        """The point of the override: an exclusive lock must be diagnosable.

        Observed in the lab — an operator left a manual session sitting in
        `configure exclusive`, and every automated run failed with netmiko's
        bare "Failed to enter configuration mode".
        """
        locked = "MINOR: MGMT_CORE #2001: configuration is locked by another session"
        conn = FakeChannel(reads=[locked], in_config_mode=[False])

        with pytest.raises(ValueError, match="locked by another session"):
            conn.config_mode()

    def test_failure_names_the_command_it_tried(self):
        conn = FakeChannel(reads=["nope"], in_config_mode=[False])

        with pytest.raises(ValueError, match="configure exclusive"):
            conn.config_mode()

    def test_classic_cli_has_no_config_mode(self):
        """No '@' in the prompt means the classic CLI, which upstream also skips."""
        conn = FakeChannel(reads=[], in_config_mode=[], base_prompt="A:core1.tst-001")

        assert conn.config_mode() == ""
        assert conn.written == []
