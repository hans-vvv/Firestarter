"""Push a rendered service snippet onto a live device, safely.

The Service Snippets page computes exactly the configuration lines a chosen set
of VPRN/VPLS objects contributes to one device (a marginal-contribution diff).
This module sends those lines to the device and commits them.

The safety property is SR OS's ``commit confirmed`` mechanism. SR OS applies the change and starts a timer; unless the change is confirmed
within that window the device reverts on its own. So the sequence is

1. open a session with the operator's credentials and enter config mode,
2. send the snippet lines — if any is rejected, stop **without committing**, so
   the candidate is discarded and the device keeps its running configuration,
3. ``commit confirmed 1`` — live now, reverts in one minute unless confirmed,
4. ``commit confirmed accept`` on the same session.

Step 4 is what proves the push did not cut us off: if the pushed configuration
broke the path this session runs over, the ``commit confirmed`` exchange or the
accept never completes, the outer handler catches it, and the device rolls itself
back when the timer expires. Locking ourselves out is therefore not a reachable
outcome — the worst case is a device that reverts to the configuration it had.

The MD-CLI mechanics: ``conn.commit()`` is a no-op through our driver, so both
commit forms are sent as ordinary config commands, and SR OS reports errors with
a severity marker rather than a non-zero status that would raise — so device
output has to be read for them (``looks_like_error`` / ``extract_device_error``).

Credentials are parameters. Nothing here reads them from the environment, a file,
or the database — the caller supplies them per run and they are never persisted.
"""

from __future__ import annotations

import logging
import traceback as tb
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from netmiko import ConnectHandler
from nornir.core.task import Result, Task

from app.automation.commands import (
    DEFAULT_WORKERS,
    build_nornir,
    run_and_disconnect,
    select_hosts,
)
from app.automation.driver import DEVICE_TYPE, register

log = logging.getLogger(__name__)

# Minutes the device waits for confirmation before rolling back. One minute is
# long enough to open a session and log in, short enough that a failed run
# leaves the device unattended for barely any time.
CONFIRM_MINUTES = 1

# Both commit forms, kept as constants because they were established by trial
# against a real device rather than from documentation.
#
# ``commit confirm accept`` is NOT valid — the device answers
# "MINOR: MGMT_CORE #2201: Unknown element - 'confirm'". The keyword is
# ``confirmed`` in both positions, which is consistent with ``commit confirmed 1``
# being accepted on the same session.
COMMIT_CONFIRMED = "commit confirmed {minutes}"
COMMIT_ACCEPT = "commit confirmed accept"

# SR OS prefixes rejected input with a severity marker rather than failing the
# command outright, so nothing raises on a bad config — it has to be read.
ERROR_MARKERS = ("MINOR:", "MAJOR:", "CRITICAL:", "Error:")


@dataclass(frozen=True)
class DeviceExchange:
    """One command sent to the device and what it answered, both redacted."""

    command: str
    output: str


def looks_like_error(output: str) -> bool:
    """True if device output carries an SR OS severity marker."""
    return any(marker in output for marker in ERROR_MARKERS)


def extract_device_error(output: str, *, redact: str | None = None) -> str:
    """Return only the severity lines from *output*, discarding everything else.

    The device echoes the command it rejected, and that command may contain a
    secret — so the raw output can never be shown or logged. The severity line
    itself is the only part worth reading::

        A:admin@core1# /configure ... password "hunter2"    <- echoed, discarded
        MINOR: SYSTEM #1372: The user password does not     <- kept
               meet the system requirement - is too simple

    *redact* is applied to whatever survives the filter, because "no SR OS
    release ever quotes the rejected value back" is an assumption about firmware
    we do not control, and the cost of being wrong is a secret in a web page.
    """
    lines = [
        line.strip()
        for line in output.splitlines()
        if any(marker in line for marker in ERROR_MARKERS)
    ]
    reason = " ".join(lines)
    if redact:
        reason = reason.replace(redact, "***")
    return reason


@dataclass(frozen=True)
class DeployStep:
    """One stage of a deploy run, for display."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class DeployResult:
    """Outcome of pushing a snippet to one device."""

    hostname: str
    ok: bool = False
    lines_pushed: int = 0
    steps: list[DeployStep] = field(default_factory=list)
    # Full device transcript. Kept whether or not the run succeeded — "it
    # committed, but what did the device actually say?" is a fair question.
    exchanges: list[DeviceExchange] = field(default_factory=list)
    error: str | None = None
    # Redacted Python traceback for an *unexpected* failure only. Expected
    # outcomes (unreachable, config rejected) carry a message and no traceback.
    traceback: str | None = None

    def add(self, name: str, *, ok: bool = True, detail: str = "") -> None:
        self.steps.append(DeployStep(name=name, ok=ok, detail=detail))


def _redact(text: str, secret: str) -> str:
    """Strip *secret* (the connection password) out of text bound for display.

    The snippet lines themselves never contain a credential, but a netmiko
    exception frequently quotes the channel data it was reading — which can
    include the password used to authenticate — so a traceback is only safe to
    show once this has run over it.
    """
    return text.replace(secret, "***") if secret else text


def deploy_config(
    *,
    hostname: str,
    address: str,
    lines: Sequence[str],
    username: str,
    password: str,
    confirm_minutes: int = CONFIRM_MINUTES,
    connect: Callable[..., Any] = ConnectHandler,
) -> DeployResult:
    """Push *lines* onto *address* and commit them, reverting on any failure.

    ``connect`` is injectable so the sequence can be tested without a device; it
    defaults to netmiko's ``ConnectHandler``.

    Never raises for an expected failure — an unreachable device, a rejected
    line, a commit the device would not accept — because every one of those
    leaves the running configuration intact (a rejected line is never committed;
    a bad commit rolls back on the timer). Returns a result whose ``steps`` say
    how far it got.
    """
    register()  # ConnectHandler resolves DEVICE_TYPE through netmiko's table
    result = DeployResult(hostname=hostname)

    # Flat-config lines arrive indented (the Printer renders a config tree);
    # strip so each is a clean MD-CLI command. Blank lines carry no config.
    commands = [line.strip() for line in lines if line.strip()]
    if not commands:
        result.error = "The selected services contribute no configuration to push."
        result.add("Nothing to push", ok=False, detail="empty snippet")
        return result

    common = {"device_type": DEVICE_TYPE, "host": address}

    try:
        session = connect(**common, username=username, password=password)
    except Exception as exc:
        result.error = f"Could not log in to {address}: {_redact(str(exc), password)}"
        result.add("Connect", ok=False, detail=_redact(str(exc), password))
        result.traceback = _redact(tb.format_exc(), password)
        return result

    result.add("Connect", detail=f"{username}@{address}")

    def _record(command: str, *, enter: bool = False) -> str:
        """Send *command*, transcribe the exchange redacted, return the output."""
        output = session.send_config_set(
            config_commands=[command],
            enter_config_mode=enter,
            exit_config_mode=False,
        )
        result.exchanges.append(
            DeviceExchange(
                command=_redact(command, password),
                output=_redact(output, password),
            )
        )
        return output

    try:
        # Send the snippet as one batch, entering config mode on the way in and
        # staying there so the commit lands against the same candidate.
        pushed = session.send_config_set(
            config_commands=commands,
            enter_config_mode=True,
            exit_config_mode=False,
        )
        result.exchanges.append(
            DeviceExchange(command="\n".join(commands), output=_redact(pushed, password))
        )
        if looks_like_error(pushed):
            reason = extract_device_error(pushed, redact=password)
            result.error = f"The device rejected the configuration; nothing was committed. {reason}"
            result.add("Push snippet", ok=False, detail=reason)
            return result
        result.lines_pushed = len(commands)
        result.add("Push snippet", detail=f"{len(commands)} line(s) staged")

        confirmed = _record(COMMIT_CONFIRMED.format(minutes=confirm_minutes))
        if looks_like_error(confirmed):
            reason = extract_device_error(confirmed, redact=password)
            result.error = f"commit confirmed {confirm_minutes} was rejected. {reason}"
            result.add("commit confirmed", ok=False, detail=reason)
            return result
        result.add(
            "commit confirmed",
            detail=f"active now, reverts in {confirm_minutes} min unless confirmed",
        )

        # The accept travels over the very session the push might have cut. If
        # the path survived, the change is made permanent; if it did not, this
        # never completes and the device rolls back when the timer expires.
        accepted = _record(COMMIT_ACCEPT)
        if looks_like_error(accepted):
            reason = extract_device_error(accepted, redact=password)
            result.error = (
                f"commit confirmed accept was rejected. {reason} "
                f"The device will roll back within {confirm_minutes} minute(s)."
            )
            result.add("commit confirmed accept", ok=False, detail=reason)
            return result

        result.add("commit confirmed accept", detail="change made permanent")
        result.ok = True
        return result

    except Exception as exc:  # unexpected: a broken session, a timeout mid-push
        log.exception("Deploy to %s failed", hostname)
        result.error = (
            f"{_redact(str(exc), password)}. If a commit was in flight the device "
            f"will restore its previous configuration within {confirm_minutes} minute(s)."
        )
        result.add("Unexpected failure", ok=False, detail=_redact(str(exc), password))
        result.traceback = _redact(tb.format_exc(), password)
        return result

    finally:
        # Closing an unconfirmed session is safe: the device is holding a timer,
        # not waiting on us.
        try:
            session.disconnect()
        except Exception:
            log.warning("Could not cleanly disconnect from %s", address)


# ---------------------------------------------------------------------------
# Batch push — many devices at once, concurrency owned by Nornir
# ---------------------------------------------------------------------------
#
# Concurrency is Nornir's throughout this codebase: reads go through
# ``commands.run_commands``' threaded runner, and a batch write goes through the
# same runner here. The push itself is still the single, commit-confirmed
# ``deploy_config`` above — Nornir supplies only the fan-out and the per-host
# inventory address, so there is exactly one write engine, not two.


def _deploy_one(
    task: Task,
    *,
    lines_by_host: Mapping[str, Sequence[str]],
    username: str,
    password: str,
    confirm_minutes: int,
) -> Result:
    """Nornir task: push one host's lines through the shared ``deploy_config``.

    ``deploy_config`` opens and drives its own session — the commit-confirmed
    sequence is defined once, there — so this task adds no device logic of its
    own. The address is the host's generated-inventory address, resolved the same
    way every other Nornir operation resolves it. The :class:`DeployResult` is
    carried back as the task result for :func:`collect_deploy_results` to gather.
    """
    address = task.host.hostname
    if address is None:
        outcome = DeployResult(
            hostname=task.host.name,
            error=f"{task.host.name} has no address in the inventory to reach it on.",
        )
    else:
        outcome = deploy_config(
            hostname=task.host.name,
            address=address,
            lines=lines_by_host[task.host.name],
            username=username,
            password=password,
            confirm_minutes=confirm_minutes,
        )
    return Result(host=task.host, result=outcome)


def collect_deploy_results(nornir_results) -> list[DeployResult]:
    """Turn Nornir's aggregated result into one :class:`DeployResult` per host.

    ``deploy_config`` already captures every *expected* failure (unreachable,
    rejected line, refused commit) as a result rather than an exception, so a
    failed Nornir task here means something *unexpected* broke before it could
    return — a connection the runner could not establish, say. That host becomes a
    failed ``DeployResult`` carrying the error, so one broken device never costs
    the batch the others' outcomes — the same guarantee ``commands.py`` makes.

    Sorted by hostname for stable rendering.
    """
    results: list[DeployResult] = []
    for hostname, multi_result in nornir_results.items():
        top = multi_result[0]
        if top.failed:
            log.warning("FAILED  %s — %s", hostname, top.exception)
            results.append(DeployResult(hostname=hostname, error=str(top.exception)))
        else:
            results.append(top.result)
    results.sort(key=lambda r: r.hostname)
    return results


def deploy_config_batch(
    *,
    lines_by_host: Mapping[str, Sequence[str]],
    username: str,
    password: str,
    inventory_dir: Path | None = None,
    num_workers: int = DEFAULT_WORKERS,
    confirm_minutes: int = CONFIRM_MINUTES,
) -> list[DeployResult]:
    """Push each host's lines concurrently, one commit-confirmed session per host.

    ``lines_by_host`` maps hostname to the lines to push to it. Hosts are taken
    from the generated Nornir inventory and narrowed to those keys, so the batch
    contacts exactly the requested devices and no others.

    Raises
    ------
    InventoryMissingError
        If the generated inventory is absent, or a requested host is not in it.
        The caller turns this into an operator-facing "rebuild the inventory"
        rather than a partial push against a stale host list.
    """
    if not lines_by_host:
        return []

    nr = select_hosts(
        build_nornir(
            username=username,
            password=password,
            inventory_dir=inventory_dir,
            num_workers=num_workers,
        ),
        list(lines_by_host),
    )
    log.info("deploying to %d device(s)", len(nr.inventory.hosts))
    # Each _deploy_one drives its own ConnectHandler and logs out in deploy_config's
    # finally, so the push sessions are already closed. close-after-run is still called
    # for symmetry with the read path and as a guard: if a pooled Nornir connection is
    # ever opened here, it too gets its logout rather than leaking.
    agg = run_and_disconnect(
        nr,
        task=_deploy_one,
        name="deploy_config_batch",
        lines_by_host=lines_by_host,
        username=username,
        password=password,
        confirm_minutes=confirm_minutes,
    )
    return collect_deploy_results(agg)
