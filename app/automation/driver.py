"""Netmiko driver for Nokia SR OS speaking MD-CLI.

Netmiko ships ``NokiaSrosSSH``, which supports both the classic CLI and MD-CLI.
Three of its assumptions do not hold against our SR OS release, all of them on
the configuration-*write* path:

1. ``config_mode`` issues ``edit-config exclusive``; our devices want
   ``configure exclusive``. Its success pattern also expects the prompt to carry
   an ``(ex)`` marker, which ours does not.
2. ``check_config_mode`` matches the literal ``(ex)[``. Ours reports config mode
   as ``[ex:``, which has to be matched as a regex — and upstream silently drops
   ``force_regex`` when it delegates to ``BaseConnection``, so the flag cannot
   simply be passed in from the caller.
3. ``_exit_all`` finishes with ``read_until_prompt``, which does not match our
   prompt after ``exit all`` and leaves the session out of sync.

Read-only use is unaffected — ``send_command`` never enters config mode — so the
config-backup path works fine on stock netmiko. This driver exists for pushing
configuration.

Registered under its own device type rather than replacing ``nokia_sros`` in
netmiko's table: a distinct name keeps the override visible in the inventory
instead of silently changing the behaviour of a stock platform.

These corrections were originally made by editing netmiko's source in place, in
a virtualenv that was later rebuilt — taking the only copy with it. Keeping them
here means they survive `pip install`, travel through git, and can be tested.
"""

from __future__ import annotations

import re

from netmiko.nokia.nokia_sros import NokiaSrosSSH
from netmiko.ssh_dispatcher import CLASS_MAPPER, platforms

# The device_type our inventory asks for. Deliberately not "nokia_sros".
DEVICE_TYPE = "nokia_sros_mdcli"

# How our MD-CLI dialect announces config mode: ``[ex:/configure]``. Upstream
# looks for ``(ex)[``, which these devices never emit. Matched as a regex, so
# the bracket is escaped.
CONFIG_MODE_MARKER = r"\[ex:"


class NokiaSrosMdCliSSH(NokiaSrosSSH):
    """Nokia SR OS driver corrected for our MD-CLI dialect.

    Only the configuration-mode methods are overridden; everything else —
    session preparation, paging, prompt discovery, ``send_command`` — is
    upstream behaviour and works as shipped.
    """

    def config_mode(
        self,
        config_command: str = "configure exclusive",
        pattern: str = "",
        re_flags: int = 0,
    ) -> str:
        """Enter config mode using ``configure exclusive``.

        The prompt we get back carries no ``(ex)`` marker, so the default
        pattern would never match; fall back to the base prompt followed by the
        ``#`` terminator instead.

        Reimplemented rather than delegated, for two reasons.

        First, upstream verifies success with ``check_config_mode()``, which
        sends a bare RETURN and looks for the marker in the reply. SR OS prints
        the context line **only on the transition** — captured from a real
        device::

            core1.tst-001# configure exclusive
            INFO: CLI #2060: Entering exclusive configuration mode
            INFO: CLI #2061: Uncommitted changes are discarded on ...

            [ex:/configure]
            A:admin@core1.tst-001#

        A later bare prompt is just ``A:admin@core1.tst-001#`` with no ``[ex:``,
        so the probe reports False while we are demonstrably in config mode, and
        upstream raises. The transition output above is proof in itself, so it
        is what gets checked — no second round-trip, nothing to get out of sync.

        Second, on failure ``BaseConnection.config_mode`` raises the bare string
        "Failed to enter configuration mode" and discards what it read. That
        output is the only thing that says *why* — most usefully when another
        session holds the exclusive lock, which is ordinary and otherwise
        undiagnosable.
        """
        if "@" not in self.base_prompt:
            # Classic CLI has no config mode; upstream skips it the same way.
            return ""

        if self.check_config_mode():
            return ""

        if not pattern:
            pattern = rf"{re.escape(self.base_prompt)}.*#"
            re_flags = re.DOTALL

        self.write_channel(self.normalize_cmd(config_command))

        output = ""
        # Read the command echo first, or it satisfies the prompt match below.
        if self.global_cmd_verify is not False:
            output += self.read_until_pattern(pattern=re.escape(config_command.strip()))
        output += self.read_until_pattern(pattern=pattern, re_flags=re_flags)

        if not re.search(CONFIG_MODE_MARKER, output):
            raise ValueError(
                f"Failed to enter configuration mode with {config_command!r}. "
                f"Device response: {output.strip()!r}"
            )
        return output

    def check_config_mode(
        self,
        check_string: str = CONFIG_MODE_MARKER,
        pattern: str = r"@",
        force_regex: bool = True,
    ) -> bool:
        """Detect config mode from the ``[ex:`` prompt marker, as a regex."""
        if "@" not in self.base_prompt:
            # Classic CLI has no config mode to be in.
            return False
        # Skip NokiaSrosSSH's own implementation: it drops force_regex when it
        # delegates, which is precisely the behaviour being corrected here.
        return super(NokiaSrosSSH, self).check_config_mode(
            check_string=check_string, pattern=pattern, force_regex=force_regex
        )

    def _exit_all(self) -> str:
        """Return to the root context, reading up to our own prompt.

        Upstream ends with ``read_until_prompt``, which does not match the
        prompt our devices emit after ``exit all`` — leaving unread data in the
        channel and desynchronising every command that follows.
        """
        output = ""
        exit_cmd = "exit all"
        self.write_channel(self.normalize_cmd(exit_cmd))
        # Read the command echo first, or the echo itself satisfies the prompt
        # match below and we fall a read behind.
        if self.global_cmd_verify is not False:
            output += self.read_until_pattern(pattern=re.escape(exit_cmd))
        output += self.read_until_pattern(pattern=rf"{re.escape(self.base_prompt)}.*#")
        return output


def register() -> None:
    """Add the driver to netmiko's device-type table. Safe to call repeatedly.

    ``ConnectHandler`` resolves ``device_type`` through ``CLASS_MAPPER``, so the
    driver has to be registered before any connection is opened. Importing this
    module is not enough — call this explicitly from whatever builds the Nornir
    inventory or opens a connection.
    """
    CLASS_MAPPER[DEVICE_TYPE] = NokiaSrosMdCliSSH
    if DEVICE_TYPE not in platforms:
        platforms.append(DEVICE_TYPE)
