from __future__ import annotations

"""Tests for the shared data pipeline (``app.pipeline``) and its web wrapper.

The collaborators (seed handler, Excel handler, snapshots, report writer) are
mocked so these tests assert orchestration — call order, the datetime-stamped
job name, and the failure-snapshot-then-reraise contract — without needing a
real database or workbook.
"""

import re
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

import app.pipeline as pipeline
import app.web.utils as web_utils
from app.pipeline import PipelineResult, run_pipeline

_JOB_NAME_RE = re.compile(r"^do_all_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")


@pytest.fixture
def mocked_pipeline(monkeypatch):
    """Replace every collaborator in ``app.pipeline`` with a MagicMock.

    Returns a namespace exposing the seed/edh instances and the module-level
    function mocks so tests can assert how they were called.
    """
    seed = MagicMock(name="SeedHandler instance")
    edh = MagicMock(name="ExcelDataHandler instance")
    monkeypatch.setattr(pipeline, "SeedHandler", MagicMock(return_value=seed))
    monkeypatch.setattr(pipeline, "ExcelDataHandler", MagicMock(return_value=edh))

    snapshot_latest = MagicMock(name="snapshot_latest")
    snapshot_failed = MagicMock(name="snapshot_failed")
    write_report_tabs = MagicMock(name="write_report_tabs")
    allocate_ce_mgmt_addresses = MagicMock(name="allocate_ce_mgmt_addresses")
    monkeypatch.setattr(pipeline, "snapshot_latest", snapshot_latest)
    monkeypatch.setattr(pipeline, "snapshot_failed", snapshot_failed)
    monkeypatch.setattr(pipeline, "write_report_tabs", write_report_tabs)
    monkeypatch.setattr(pipeline, "allocate_ce_mgmt_addresses", allocate_ce_mgmt_addresses)

    return MagicMock(
        seed=seed,
        edh=edh,
        snapshot_latest=snapshot_latest,
        snapshot_failed=snapshot_failed,
        write_report_tabs=write_report_tabs,
        allocate_ce_mgmt_addresses=allocate_ce_mgmt_addresses,
    )


class TestRunPipeline:
    def test_success_returns_result_with_datetime_job_name(self, mocked_pipeline):
        result = run_pipeline(session=MagicMock(name="session"))

        assert isinstance(result, PipelineResult)
        assert _JOB_NAME_RE.match(result.job_name)
        assert result.finished_at >= result.started_at
        assert result.duration_seconds >= 0

    def test_execute_job_gets_the_same_datetime_name(self, mocked_pipeline):
        result = run_pipeline(session=MagicMock(name="session"))
        # The Job the pipeline builds carries exactly the returned name, so the
        # audit row and the reported run line up.
        assert mocked_pipeline.edh.execute_job.call_args.kwargs["job_name"] == result.job_name

    def test_success_snapshots_latest_and_writes_reports(self, mocked_pipeline):
        run_pipeline(session=MagicMock(name="session"))

        mocked_pipeline.snapshot_latest.assert_called_once()
        mocked_pipeline.write_report_tabs.assert_called_once()
        mocked_pipeline.snapshot_failed.assert_not_called()

    def test_success_allocates_ce_mgmt_addresses_before_reports(self, mocked_pipeline):
        # The CE management-IP allocator must populate CeMgmtAddress before the
        # report tab (which reads it) is written.
        calls: list[str] = []
        mocked_pipeline.allocate_ce_mgmt_addresses.side_effect = lambda **_: calls.append(
            "allocate"
        )
        mocked_pipeline.write_report_tabs.side_effect = lambda **_: calls.append("report")

        run_pipeline(session=MagicMock(name="session"))

        mocked_pipeline.allocate_ce_mgmt_addresses.assert_called_once()
        assert calls == ["allocate", "report"]

    def test_never_wipes_the_database(self, mocked_pipeline):
        run_pipeline(session=MagicMock(name="session"))
        mocked_pipeline.edh.wipe_db.assert_not_called()

    def test_failure_snapshots_failed_and_reraises(self, mocked_pipeline):
        boom = RuntimeError("compute blew up")
        mocked_pipeline.edh.compute_services.side_effect = boom

        with pytest.raises(RuntimeError, match="compute blew up"):
            run_pipeline(session=MagicMock(name="session"))

        mocked_pipeline.snapshot_failed.assert_called_once()
        mocked_pipeline.snapshot_latest.assert_not_called()


class TestRunDataPipeline:
    """The web wrapper: records the outcome and re-raises on failure."""

    @pytest.fixture(autouse=True)
    def reset_last_run(self):
        web_utils._pipeline_last_run = web_utils.PipelineLastRun()

    @pytest.fixture
    def fake_session(self, monkeypatch):
        @contextmanager
        def _dummy():
            yield MagicMock(name="session")

        monkeypatch.setattr(web_utils, "db_session", _dummy)

    def test_success_records_ok_last_run(self, monkeypatch, fake_session):
        result = PipelineResult(
            job_name="do_all_2026-06-27_10-00-00",
            started_at=web_utils.datetime.now(web_utils.UTC),
            finished_at=web_utils.datetime.now(web_utils.UTC),
        )
        monkeypatch.setattr(web_utils, "run_pipeline", lambda *, session: result)

        returned = web_utils.run_data_pipeline()

        assert returned is result
        last = web_utils.get_pipeline_last_run()
        assert last.status == "ok"
        assert last.job_name == "do_all_2026-06-27_10-00-00"
        assert last.traceback is None

    def test_failure_records_traceback_and_reraises(self, monkeypatch, fake_session):
        def _boom(*, session):
            raise ValueError("validation failed")

        monkeypatch.setattr(web_utils, "run_pipeline", _boom)

        with pytest.raises(ValueError, match="validation failed"):
            web_utils.run_data_pipeline()

        last = web_utils.get_pipeline_last_run()
        assert last.status == "failed"
        assert "validation failed" in last.traceback
        assert last.job_name is None
