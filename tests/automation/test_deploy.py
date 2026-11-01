"""Tests for pushing a rendered snippet onto a device.

``deploy_config`` takes its connection factory as an argument, so the whole
sequence runs here against a fake that records what it was sent. No device, no
SSH.

The properties worth pinning are the safety ones: a rejected line is never
committed, a rejected commit is reported (and the device left to roll itself
back), and the connection password never leaks into anything the caller might
display or log.
"""

from __future__ import annotations

from app.automation.deploy import COMMIT_ACCEPT, COMMIT_CONFIRMED, CONFIRM_MINUTES, deploy_config

CONN_PASSWORD = "conn-s3cr3t!"

# Indented flat-config lines, as the Printer renders them. Deploy must strip the
# indentation before sending and drop the blank line.
SNIPPET_LINES = [
    '    configure { router "Base" interface "to-ce" admin-state enable }',
    "",
    '    configure { service vprn "CUST-A" customer "1" }',
]
STRIPPED = [
    'configure { router "Base" interface "to-ce" admin-state enable }',
    'configure { service vprn "CUST-A" customer "1" }',
]


class FakeSession:
    """Records every command it is sent; supports both batch and single sends."""

    def __init__(self, *, username, password, host, fail_on=None):
        self.username = username
        self.password = password
        self.host = host
        self.sent: list[str] = []
        self.disconnected = False
        self._fail_on = fail_on or {}

    def send_config_set(self, *, config_commands, enter_config_mode, exit_config_mode):
        assert exit_config_mode is False, "the session must stay in config mode"
        self.sent.extend(config_commands)
        for command in config_commands:
            for needle, response in self._fail_on.items():
                if needle in command:
                    return response
        return "\n".join(f"{c}\n(ok)" for c in config_commands)

    def disconnect(self):
        self.disconnected = True


class Connector:
    """Stands in for ConnectHandler; hands out FakeSessions and logs each login."""

    def __init__(self, *, accepts=None, fail_on=None, raise_on_connect=None):
        self.accepts = accepts
        self.fail_on = fail_on
        self.raise_on_connect = raise_on_connect
        self.logins: list[tuple[str, str]] = []
        self.sessions: list[FakeSession] = []

    def __call__(self, *, device_type, host, username, password):
        self.logins.append((username, password))
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        if self.accepts is not None and self.accepts.get(username) != password:
            raise OSError("Authentication failed")
        session = FakeSession(username=username, password=password, host=host, fail_on=self.fail_on)
        self.sessions.append(session)
        return session


def _run(connector, *, lines=None, **kwargs):
    return deploy_config(
        hostname="core1.tst-001",
        address="10.201.4.3",
        lines=SNIPPET_LINES if lines is None else lines,
        username="admin",
        password=CONN_PASSWORD,
        connect=connector,
        **kwargs,
    )


class TestHappyPath:
    def test_reports_success(self):
        result = _run(Connector())
        assert result.ok is True
        assert result.error is None

    def test_lines_pushed_counts_only_real_lines(self):
        result = _run(Connector())
        assert result.lines_pushed == len(STRIPPED)

    def test_pushes_stripped_lines_then_commits_confirmed_then_accepts(self):
        connector = Connector()
        _run(connector)

        sent = connector.sessions[0].sent
        assert sent == [
            *STRIPPED,
            COMMIT_CONFIRMED.format(minutes=CONFIRM_MINUTES),
            COMMIT_ACCEPT,
        ]

    def test_indentation_is_stripped_before_sending(self):
        connector = Connector()
        _run(connector)
        for line in STRIPPED:
            assert line in connector.sessions[0].sent
        assert not any(c.startswith(" ") for c in connector.sessions[0].sent)

    def test_session_is_closed(self):
        connector = Connector()
        _run(connector)
        assert connector.sessions[0].disconnected is True

    def test_steps_cover_the_whole_sequence(self):
        result = _run(Connector())
        names = [s.name for s in result.steps]
        assert names == ["Connect", "Push snippet", "commit confirmed", "commit confirmed accept"]
        assert all(s.ok for s in result.steps)


class TestNothingToPush:
    def test_empty_lines_never_open_a_session(self):
        connector = Connector()
        result = _run(connector, lines=["", "   "])
        assert result.ok is False
        assert connector.logins == []
        assert "no configuration" in result.error.lower()


class TestConnectFails:
    def test_unreachable_device_is_reported_not_raised(self):
        connector = Connector(accepts={})  # nothing authenticates
        result = _run(connector)
        assert result.ok is False
        assert "could not log in" in result.error.lower()

    def test_carries_a_redacted_traceback(self):
        connector = Connector(raise_on_connect=OSError(f"refused with {CONN_PASSWORD}"))
        result = _run(connector)
        assert result.traceback is not None
        assert CONN_PASSWORD not in result.traceback
        assert CONN_PASSWORD not in result.error


class TestDeviceRejectsTheConfig:
    def test_rejected_line_stops_before_committing(self):
        connector = Connector(fail_on={"vprn": "MINOR: MGMT_CORE #2201: bad element"})
        result = _run(connector)

        assert result.ok is False
        sent = connector.sessions[0].sent
        assert COMMIT_CONFIRMED.format(minutes=CONFIRM_MINUTES) not in sent
        assert COMMIT_ACCEPT not in sent
        assert "rejected" in result.error.lower()

    def test_rejected_commit_confirmed_is_reported(self):
        connector = Connector(fail_on={"commit confirmed 1": "MINOR: commit failed"})
        result = _run(connector)
        assert result.ok is False
        assert COMMIT_ACCEPT not in connector.sessions[0].sent
        assert "commit confirmed" in result.error

    def test_rejected_accept_warns_about_rollback(self):
        connector = Connector(fail_on={"commit confirmed accept": "MAJOR: accept rejected"})
        result = _run(connector)
        assert result.ok is False
        assert "roll back" in result.error.lower()

    def test_session_is_still_closed_after_a_rejection(self):
        connector = Connector(fail_on={"vprn": "MINOR: bad"})
        _run(connector)
        assert connector.sessions[0].disconnected is True


class TestUnexpectedFailure:
    def test_mid_push_exception_is_caught_and_reported(self):
        class Exploding(FakeSession):
            def send_config_set(self, **kwargs):
                raise RuntimeError(f"channel died holding {CONN_PASSWORD}")

        class ExplodingConnector(Connector):
            def __call__(self, *, device_type, host, username, password):
                self.logins.append((username, password))
                s = Exploding(username=username, password=password, host=host)
                self.sessions.append(s)
                return s

        connector = ExplodingConnector()
        result = _run(connector)
        assert result.ok is False
        assert "roll back" in result.error.lower() or "restore" in result.error.lower()
        assert CONN_PASSWORD not in repr(result)
        assert connector.sessions[0].disconnected is True


class TestPasswordIsNeverLeaked:
    def test_absent_from_a_successful_result(self):
        result = _run(Connector())
        assert CONN_PASSWORD not in repr(result)


# ---------------------------------------------------------------------------
# Batch push — the pure seams (Nornir's device call itself is not exercised,
# same convention as test_commands.py)
# ---------------------------------------------------------------------------

from types import SimpleNamespace

from app.automation import deploy as deploy_mod
from app.automation.deploy import (
    DeployResult,
    _deploy_one,
    collect_deploy_results,
    deploy_config_batch,
)


def _host(name, hostname="10.0.0.1"):
    return SimpleNamespace(name=name, hostname=hostname)


def _task(name, hostname="10.0.0.1"):
    return SimpleNamespace(host=_host(name, hostname))


def _multi(*, failed, result=None, exception=None):
    """One Nornir MultiResult: a list whose [0] is the task's top Result."""
    return [SimpleNamespace(failed=failed, result=result, exception=exception)]


class TestCollectDeployResults:
    def test_successful_task_result_is_returned_verbatim(self):
        r = DeployResult(hostname="pe1", ok=True, lines_pushed=2)
        out = collect_deploy_results({"pe1": _multi(failed=False, result=r)})
        assert out == [r]

    def test_failed_task_becomes_an_errored_result(self):
        out = collect_deploy_results(
            {"pe1": _multi(failed=True, exception=OSError("connection refused"))}
        )
        assert len(out) == 1
        assert out[0].hostname == "pe1"
        assert out[0].ok is False
        assert "connection refused" in out[0].error

    def test_one_failure_does_not_lose_the_others(self):
        ok1 = DeployResult(hostname="pe1", ok=True)
        ok3 = DeployResult(hostname="pe3", ok=True)
        out = collect_deploy_results(
            {
                "pe1": _multi(failed=False, result=ok1),
                "pe2": _multi(failed=True, exception=OSError("boom")),
                "pe3": _multi(failed=False, result=ok3),
            }
        )
        assert [r.hostname for r in out] == ["pe1", "pe2", "pe3"]
        assert [r.ok for r in out] == [True, False, True]

    def test_results_are_sorted_by_hostname(self):
        out = collect_deploy_results(
            {
                "pe3": _multi(failed=False, result=DeployResult(hostname="pe3", ok=True)),
                "pe1": _multi(failed=False, result=DeployResult(hostname="pe1", ok=True)),
            }
        )
        assert [r.hostname for r in out] == ["pe1", "pe3"]


class TestDeployOneTask:
    def test_delegates_hostname_address_and_lines_to_deploy_config(self, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(
            deploy_mod,
            "deploy_config",
            lambda **kw: seen.update(kw) or DeployResult(hostname=kw["hostname"], ok=True),
        )
        res = _deploy_one(
            _task("pe1", "10.9.9.9"),
            lines_by_host={"pe1": ["configure a", "configure b"]},
            username="netops",
            password="pw",
            confirm_minutes=1,
        )
        assert res.result.ok is True
        assert seen["hostname"] == "pe1"
        assert seen["address"] == "10.9.9.9"
        assert seen["lines"] == ["configure a", "configure b"]
        assert seen["username"] == "netops"
        assert seen["password"] == "pw"

    def test_host_without_an_address_is_a_clean_error_not_a_push(self, monkeypatch):
        monkeypatch.setattr(
            deploy_mod,
            "deploy_config",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not push")),
        )
        res = _deploy_one(
            _task("pe1", hostname=None),
            lines_by_host={"pe1": ["configure a"]},
            username="u",
            password="p",
            confirm_minutes=1,
        )
        assert res.result.ok is False
        assert "no address" in res.result.error


class _FakeNornir:
    def __init__(self):
        self.inventory = SimpleNamespace(hosts={"pe1": object()})
        self.events = []
        self.closed_with = None

    def run(self, **kwargs):
        self.events.append("run")
        return {}

    def close_connections(self, on_good=True, on_failed=False):
        self.events.append("close")
        self.closed_with = (on_good, on_failed)


class TestDeployConfigBatch:
    def test_empty_input_contacts_nothing(self, monkeypatch):
        monkeypatch.setattr(
            deploy_mod,
            "build_nornir",
            lambda **kw: (_ for _ in ()).throw(AssertionError("must not build Nornir")),
        )
        assert deploy_config_batch(lines_by_host={}, username="u", password="p") == []

    def test_closes_connections_after_the_batch_run(self, monkeypatch):
        # Symmetry with the read path: the batch's Nornir instance is closed out
        # after the run, so any pooled connection gets its logout rather than leaking.
        nr = _FakeNornir()
        monkeypatch.setattr(deploy_mod, "build_nornir", lambda **kw: nr)
        monkeypatch.setattr(deploy_mod, "select_hosts", lambda n, hosts: n)

        deploy_config_batch(lines_by_host={"pe1": ["configure a"]}, username="u", password="p")
        assert nr.events == ["run", "close"]
        assert nr.closed_with == (True, True)
