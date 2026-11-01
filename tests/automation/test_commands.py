"""Tests for the general Nornir command runner.

Same split as ``test_backup.py``: the parts that talk to devices
(``build_nornir``, ``run_commands``' Nornir call) are not exercised, and the
pure seams around them are. ``collect_command_results`` is where a
misinterpreted result would quietly corrupt everything downstream, so that is
where the tests concentrate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.automation.commands import (
    CommandResult,
    InventoryMissingError,
    collect_command_results,
    run_commands,
    select_hosts,
)

SHOW_A = "show router isis database detail"
SHOW_B = "show router interface"


def _ok(*outputs):
    return [SimpleNamespace(failed=False, result=list(outputs), exception=None)]


def _failed(exc):
    return [SimpleNamespace(failed=True, result=None, exception=exc)]


class TestCollectCommandResults:
    def test_output_is_paired_with_the_command_that_produced_it(self):
        collected = collect_command_results(
            {"core1": _ok("isis output", "interface output")},
            commands=[SHOW_A, SHOW_B],
        )
        by_command = {r.command: r.output for r in collected["core1"]}
        assert by_command == {SHOW_A: "isis output", SHOW_B: "interface output"}

    def test_order_follows_the_requested_commands(self):
        collected = collect_command_results(
            {"core1": _ok("first", "second")},
            commands=[SHOW_A, SHOW_B],
        )
        assert [r.command for r in collected["core1"]] == [SHOW_A, SHOW_B]

    def test_failed_host_yields_one_errored_result_per_command(self):
        """Not a short list — callers index by command without special-casing."""
        collected = collect_command_results(
            {"core2": _failed(OSError("unreachable"))},
            commands=[SHOW_A, SHOW_B],
        )
        assert len(collected["core2"]) == 2
        assert [r.ok for r in collected["core2"]] == [False, False]
        assert all("unreachable" in r.error for r in collected["core2"])

    def test_failed_host_carries_no_output(self):
        """A missing capture must never be mistakable for a real, empty response."""
        collected = collect_command_results({"core2": _failed(OSError("boom"))}, commands=[SHOW_A])
        assert collected["core2"][0].output is None

    def test_one_failure_does_not_lose_the_others(self):
        collected = collect_command_results(
            {
                "core1": _ok("A"),
                "core2": _failed(OSError("boom")),
                "core3": _ok("C"),
            },
            commands=[SHOW_A],
        )
        assert sorted(collected) == ["core1", "core2", "core3"]
        assert collected["core1"][0].output == "A"
        assert collected["core3"][0].output == "C"

    def test_mismatched_output_count_is_not_silently_accepted(self):
        """zip(strict=True): a device returning fewer outputs than commands is a bug.

        Pairing by position with a short list would silently attribute one
        command's output to another.
        """
        with pytest.raises(ValueError, match="argument"):
            collect_command_results({"core1": _ok("only one")}, commands=[SHOW_A, SHOW_B])


class TestSelectHosts:
    @staticmethod
    def _nornir(*hostnames):
        filtered = {}

        def _filter(*, filter_func):
            kept = [h for h in hostnames if filter_func(SimpleNamespace(name=h))]
            filtered["kept"] = kept
            return SimpleNamespace(inventory=SimpleNamespace(hosts=dict.fromkeys(kept)))

        return SimpleNamespace(
            inventory=SimpleNamespace(hosts=dict.fromkeys(hostnames)),
            filter=_filter,
        ), filtered

    def test_none_returns_the_inventory_untouched(self):
        nr, _ = self._nornir("core1", "core2")
        assert select_hosts(nr, None) is nr

    def test_narrows_to_the_requested_hosts(self):
        nr, filtered = self._nornir("core1", "core2", "pe1")
        select_hosts(nr, ["core1", "core2"])
        assert filtered["kept"] == ["core1", "core2"]

    def test_unknown_host_raises_rather_than_running_against_nothing(self):
        """Silently matching zero devices would look like a clean run that did nothing."""
        nr, _ = self._nornir("core1")
        with pytest.raises(InventoryMissingError, match="core9"):
            select_hosts(nr, ["core1", "core9"])

    def test_the_error_names_every_unknown_host(self):
        nr, _ = self._nornir("core1")
        with pytest.raises(InventoryMissingError) as exc:
            select_hosts(nr, ["core8", "core9"])
        assert "core8" in str(exc.value)
        assert "core9" in str(exc.value)


class _FakeNornir:
    """Records the order of run/close so a test can assert the teardown happened."""

    def __init__(self, *, run_result=None, run_raises=None):
        self.inventory = SimpleNamespace(hosts={"core1": object()})
        self.events: list[str] = []
        self.closed_with: tuple[bool, bool] | None = None
        self._run_result = run_result if run_result is not None else {}
        self._run_raises = run_raises

    def run(self, **kwargs):
        self.events.append("run")
        if self._run_raises is not None:
            raise self._run_raises
        return self._run_result

    def close_connections(self, on_good=True, on_failed=False):
        self.events.append("close")
        self.closed_with = (on_good, on_failed)


class TestRunAndDisconnect:
    def test_closes_connections_after_the_run(self):
        from app.automation.commands import run_and_disconnect

        nr = _FakeNornir(run_result={"core1": _ok("out")})
        result = run_and_disconnect(nr, task=object(), name="x")
        assert result is nr._run_result  # the run's result is returned unchanged
        assert nr.events == ["run", "close"]

    def test_closes_on_both_good_and_failed_hosts(self):
        # A host that raised after connecting still holds a session to log out of.
        from app.automation.commands import run_and_disconnect

        nr = _FakeNornir()
        run_and_disconnect(nr, task=object(), name="x")
        assert nr.closed_with == (True, True)

    def test_closes_even_when_the_task_raises(self):
        from app.automation.commands import run_and_disconnect

        nr = _FakeNornir(run_raises=RuntimeError("runner blew up"))
        with pytest.raises(RuntimeError, match="runner blew up"):
            run_and_disconnect(nr, task=object(), name="x")
        assert nr.events == ["run", "close"]  # close still ran in finally


class TestRunCommandsClosesConnections:
    def test_run_commands_closes_the_nornir_connections(self, monkeypatch):
        # The real leak: run_commands used to call nr.run and walk away, leaving an
        # open SSH session on every device. It must close them (→ logout) afterwards.
        import app.automation.commands as commands_mod

        nr = _FakeNornir(run_result={})
        monkeypatch.setattr(commands_mod, "build_nornir", lambda **kw: nr)
        monkeypatch.setattr(commands_mod, "select_hosts", lambda n, h: n)

        run_commands(username="u", password="p", commands=[SHOW_A])
        assert nr.events == ["run", "close"]
        assert nr.closed_with == (True, True)


class TestRunCommandsValidation:
    def test_empty_command_list_is_rejected_before_connecting(self):
        """Fail on the argument, not after opening 20 SSH sessions to do nothing."""
        with pytest.raises(ValueError, match="no commands"):
            run_commands(username="u", password="p", commands=[])


class TestCommandResult:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [(None, True), ("timeout", False), ("", False)],
    )
    def test_ok_is_derived_from_error(self, error, expected):
        # Mirrors FetchResult: `ok` tests `is None`, not truthiness, so an empty
        # error message still marks the command as failed.
        result = CommandResult(hostname="h", command=SHOW_A, output="x", error=error)
        assert result.ok is expected
