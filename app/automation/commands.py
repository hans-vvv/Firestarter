"""Run show commands against inventory devices and capture their output.

The general form of what :mod:`app.automation.backup` was already doing for a
single hard-coded command. Anything that needs to read something off a device —
the compliance config fetch, the IS-IS database scan behind the State tab, and
whatever comes next — goes through here rather than opening its own Nornir
session.

Two rules carried over from the backup module, because they are what make the
results trustworthy:

* **Failures are collected, never raised.** One unreachable device must not cost
  us the output of every other device in the run.
* **A failed device produces no output at all**, only an error. A partial or
  empty capture must never be mistakable for a real response.

Credentials are parameters. Nothing here reads them from the environment, a
file, or the database — the caller supplies them per run (the CLI prompts, the
dashboard asks in a dialog) and they are never persisted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import Result, Task

from app.automation.driver import register
from app.automation.inventory import DEFAULT_OUT_DIR, GROUPS_FILENAME, HOSTS_FILENAME

log = logging.getLogger(__name__)

# Matches the backup run's concurrency. Twenty parallel SSH sessions is
# comfortable for a lab; revisit before pointing this at a production estate.
DEFAULT_WORKERS = 20


class InventoryMissingError(RuntimeError):
    """The generated inventory is absent, or does not contain a requested host."""


@dataclass(frozen=True)
class CommandResult:
    """Output of one command on one device, or the error that prevented it."""

    hostname: str
    command: str
    output: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the command returned output.

        Derived rather than stored so it cannot contradict ``error`` — the same
        reasoning as ``FetchResult.ok``.
        """
        return self.error is None


def build_nornir(
    *,
    username: str,
    password: str,
    inventory_dir: Path | None = None,
    num_workers: int = DEFAULT_WORKERS,
) -> Nornir:
    """Build a Nornir instance from the generated inventory.

    The inventory is assembled programmatically from absolute paths rather than
    a checked-in ``config.yaml``, so this behaves identically no matter which
    directory the caller was invoked from.

    Raises
    ------
    InventoryMissingError
        If the generated inventory files do not exist. They are derived state
        and deliberately not carried in the data bundle, so a freshly seeded
        checkout has none until the inventory is rebuilt.
    """
    register()  # the inventory names our device type; make netmiko aware of it

    inv = inventory_dir or DEFAULT_OUT_DIR
    hosts_file = inv / HOSTS_FILENAME
    groups_file = inv / GROUPS_FILENAME
    for path in (hosts_file, groups_file):
        if not path.exists():
            raise InventoryMissingError(
                f"Inventory file not found: {path}. Rebuild the Nornir inventory first."
            )

    nr = InitNornir(
        inventory={
            "plugin": "SimpleInventory",
            "options": {"host_file": str(hosts_file), "group_file": str(groups_file)},
        },
        runner={"plugin": "threaded", "options": {"num_workers": num_workers}},
        logging={"enabled": False},
    )
    nr.inventory.defaults.username = username
    nr.inventory.defaults.password = password
    return nr


def select_hosts(nr: Nornir, hostnames: Sequence[str] | None) -> Nornir:
    """Narrow *nr* to *hostnames*, or return it unchanged when None.

    An unknown hostname is an error rather than a silent no-op: the caller asked
    for a specific device, and quietly running against nothing (or against
    everything) would both be worse than saying so. The usual cause is an
    inventory that predates a topology change.
    """
    if hostnames is None:
        return nr

    known = set(nr.inventory.hosts)
    unknown = sorted(set(hostnames) - known)
    if unknown:
        raise InventoryMissingError(
            f"Not in the generated inventory: {', '.join(unknown)}. "
            "Rebuild the Nornir inventory, or check the device is active."
        )

    wanted = set(hostnames)
    return nr.filter(filter_func=lambda host: host.name in wanted)


def run_and_disconnect(nr: Nornir, **run_kwargs):
    """Run a Nornir task, then **always** close every connection it opened.

    Nornir keeps each host's netmiko session in a pool and never closes it on its
    own — the caller has to. Skipping that leaves an SSH session open on every
    device the run touched; on SR OS those linger until the VTY idle-timeout,
    so a routine compliance fetch or IS-IS scan quietly accumulates stale
    sessions. Closing routes through ``nornir_netmiko``'s ``close`` →
    ``disconnect`` → the Nokia driver's ``cleanup``, which sends ``logout`` — the
    clean teardown the device expects.

    ``on_failed=True`` as well: a host whose task raised *after* connecting still
    holds an open session, and it needs the same ``logout``. The close runs in a
    ``finally`` so an exception from the task itself never skips it.
    """
    try:
        return nr.run(**run_kwargs)
    finally:
        nr.close_connections(on_good=True, on_failed=True)


def _send_commands(task: Task, *, commands: Sequence[str]) -> Result:
    """Nornir task: send each command down one connection, in order."""
    conn = task.host.get_connection("netmiko", task.nornir.config)
    return Result(host=task.host, result=[conn.send_command(command) for command in commands])


def _send_host_commands(task: Task, *, commands_by_host: dict[str, Sequence[str]]) -> Result:
    """Nornir task: send *this host's* own command list down one connection.

    The per-host variant of :func:`_send_commands`. Every host runs the same
    task, but each pulls the commands meant for it — so one concurrent run can
    ask different devices different things (e.g. each device's own set of
    transceiver ports).
    """
    conn = task.host.get_connection("netmiko", task.nornir.config)
    commands = commands_by_host[task.host.name]
    return Result(host=task.host, result=[conn.send_command(command) for command in commands])


def collect_command_results(
    nornir_results,
    *,
    commands: Sequence[str],
) -> dict[str, list[CommandResult]]:
    """Turn Nornir's aggregated result into ``CommandResult`` per host per command.

    A host that failed yields one errored ``CommandResult`` per requested
    command, not a short list. Callers can then index by command without
    special-casing failures, and a missing entry always means "not asked for"
    rather than "asked for and lost".

    The task sends commands on a single connection, so a failure part-way
    through leaves Nornir holding only the exception — there is no partial
    output to salvage. Marking every command failed is therefore the honest
    reading, not a simplification.
    """
    collected: dict[str, list[CommandResult]] = {}

    for hostname, multi_result in nornir_results.items():
        top = multi_result[0]

        if top.failed:
            log.warning("FAILED  %s — %s", hostname, top.exception)
            collected[hostname] = [
                CommandResult(hostname=hostname, command=command, error=str(top.exception))
                for command in commands
            ]
            continue

        outputs = top.result
        log.info("ok      %s — %d command(s)", hostname, len(outputs))
        collected[hostname] = [
            CommandResult(hostname=hostname, command=command, output=output)
            for command, output in zip(commands, outputs, strict=True)
        ]

    return collected


def run_commands(
    *,
    username: str,
    password: str,
    commands: Sequence[str],
    hostnames: Sequence[str] | None = None,
    inventory_dir: Path | None = None,
    num_workers: int = DEFAULT_WORKERS,
) -> dict[str, list[CommandResult]]:
    """Run *commands* on every inventory device, or just on *hostnames*.

    Returns a mapping of hostname to one ``CommandResult`` per command, in the
    order the commands were given.
    """
    if not commands:
        raise ValueError("no commands given")

    nr = select_hosts(
        build_nornir(
            username=username,
            password=password,
            inventory_dir=inventory_dir,
            num_workers=num_workers,
        ),
        hostnames,
    )

    log.info("running %d command(s) on %d device(s)", len(commands), len(nr.inventory.hosts))
    results = run_and_disconnect(nr, task=_send_commands, name="send_commands", commands=commands)
    return collect_command_results(results, commands=commands)


def run_commands_per_host(
    *,
    username: str,
    password: str,
    commands_by_host: dict[str, Sequence[str]],
    inventory_dir: Path | None = None,
    num_workers: int = DEFAULT_WORKERS,
) -> dict[str, list[CommandResult]]:
    """Run a *different* command list on each host, all in one concurrent run.

    Where :func:`run_commands` sends one shared command list to every host, this
    lets each host get its own — the natural fit when what to ask depends on the
    device (e.g. each device's own set of transceiver ports). Hosts absent from
    *commands_by_host* are not contacted; every host named in it must exist in the
    generated inventory, same as :func:`select_hosts`.

    Returns a mapping of hostname to one ``CommandResult`` per command, in the
    order that host's commands were given. Failures are collected per host, never
    raised — identical semantics to :func:`run_commands`.
    """
    if not commands_by_host:
        raise ValueError("no commands given")

    nr = select_hosts(
        build_nornir(
            username=username,
            password=password,
            inventory_dir=inventory_dir,
            num_workers=num_workers,
        ),
        list(commands_by_host),
    )

    log.info("running per-host commands on %d device(s)", len(nr.inventory.hosts))
    results = run_and_disconnect(
        nr, task=_send_host_commands, name="send_host_commands", commands_by_host=commands_by_host
    )

    collected: dict[str, list[CommandResult]] = {}
    for hostname, multi_result in results.items():
        commands = commands_by_host[hostname]
        top = multi_result[0]
        if top.failed:
            log.warning("FAILED  %s — %s", hostname, top.exception)
            collected[hostname] = [
                CommandResult(hostname=hostname, command=command, error=str(top.exception))
                for command in commands
            ]
            continue
        collected[hostname] = [
            CommandResult(hostname=hostname, command=command, output=output)
            for command, output in zip(commands, top.result, strict=True)
        ]
    return collected
