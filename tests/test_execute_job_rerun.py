from __future__ import annotations

"""Re-run semantics of ExcelDataHandler.execute_job.

When the operator adds a row to an Excel sheet (a new PE device,
say) and re-runs the topology pipeline, the freshly built
``actions_blob`` must replace whatever the existing Job row carries —
otherwise the executor silently replays the original first-run plan
and the new row is dropped on the floor.
"""

import pytest

from app.excel_data_handling.excel_data_handler import ExcelDataHandler
from app.models import Job
from app.repositories.job import get_job_by_name
from app.services.job_handling.job_executor import JobExecutor
from tests.conftest import WB_NAME


def test_execute_job_refreshes_actions_blob_on_existing_job(
    session,
    monkeypatch,
):
    """When execute_job re-runs against an existing Job row, the
    executor must see the freshly built actions_blob and the persisted
    Job.actions_blob must be updated to match."""

    # Pre-stage an existing job with a stale plan (one old step).
    old_step = {"action": "noop_old", "params": {}}
    job = Job(name="rerun_test", actions_blob=[old_step])
    session.add(job)
    session.flush()

    # Capture what the executor actually sees, and skip running it for real.
    captured: dict = {}

    def fake_execute(self_, job_arg):
        captured["seen_blob"] = list(job_arg.actions_blob)
        return False

    monkeypatch.setattr(JobExecutor, "execute", fake_execute)

    # Build a fresh blob and execute under the same job name.
    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)
    new_step = {"action": "noop_new", "params": {}}
    edh.actions_blob = [new_step]

    edh.execute_job(job_name="rerun_test")

    # The executor must have seen the new blob, not the stored old one.
    assert captured["seen_blob"] == [new_step]

    # And the persisted Job row must now carry the new blob — so a
    # subsequent get_job_by_name caller (or a snapshot/restart) sees
    # the same plan that was just executed.
    session.expire(job)
    refreshed = get_job_by_name(session, "rerun_test")
    assert refreshed.actions_blob == [new_step]


def test_execute_job_creates_job_when_none_exists(session, monkeypatch):
    """First run: no Job row yet. The new Job is created with the
    current blob and seen by the executor."""

    captured: dict = {}

    def fake_execute(self_, job_arg):
        captured["seen_blob"] = list(job_arg.actions_blob)
        return False

    monkeypatch.setattr(JobExecutor, "execute", fake_execute)

    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)
    step = {"action": "noop", "params": {}}
    edh.actions_blob = [step]

    edh.execute_job(job_name="first_run_job")

    assert captured["seen_blob"] == [step]
    created = get_job_by_name(session, "first_run_job")
    assert created is not None
    assert created.actions_blob == [step]


def test_execute_job_clears_handler_blob_after_run(session, monkeypatch):
    """``self.actions_blob`` is cleared at the end so the next batch
    of create_actions_blob_for_* calls starts from an empty slate."""

    monkeypatch.setattr(JobExecutor, "execute", lambda self_, j: False)

    edh = ExcelDataHandler(session=session, wb_name=WB_NAME)
    edh.actions_blob = [{"action": "noop", "params": {}}]

    edh.execute_job(job_name="clear_test")

    assert edh.actions_blob == []
