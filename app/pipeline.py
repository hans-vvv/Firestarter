from __future__ import annotations

"""Single source of truth for the end-to-end ingestion + service pipeline.

This is the sequence that turns the on-disk Excel workbook into computed,
renderable device configuration: validate the workbook, seed reference data,
build the topology from the actions blob, then run the
service orchestrator and write the report tabs / snapshot.

It is deliberately idempotent — every step is safe to re-run (seeds skip
existing rows, the executor's per-action checks skip existing topology) — so it
can be triggered repeatedly from either the CLI (``main.py``) or the dashboard
without wiping the database. ``wipe_db()`` is intentionally NOT part of this
function: a pipeline run never destroys existing state. Rebuilding from an empty
database stays a deliberate, manual bootstrap step.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.excel_data_handling.excel_data_handler import ExcelDataHandler
from app.excel_data_handling.reports import write_report_tabs
from app.excel_data_handling.seed import SeedHandler
from app.excel_data_handling.snapshots import snapshot_failed, snapshot_latest
from app.services.service_handling.ce_mgmt_allocator import allocate_ce_mgmt_addresses


@dataclass(frozen=True)
class PipelineResult:
    """Outcome of a single successful pipeline run."""

    job_name: str
    started_at: datetime
    finished_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def run_pipeline(*, session: Session) -> PipelineResult:
    """Run the full ingestion + service-computation pipeline on ``session``.

    The topology-build Job is named ``do_all_<UTC-timestamp>`` so every run is
    persisted as its own row — an audit trail of when the pipeline last ran.
    Because the name is unique per run, ``execute_job`` builds the job from the
    freshly computed actions blob each time, so new Excel rows are always picked
    up (the executor's per-action idempotency skips topology that already
    exists).

    On any failure the current Excel + DB + YAML definitions are snapshotted to
    ``state_snapshots/failed/<ts>`` before the exception propagates, so the exact
    inputs of a broken run are preserved for troubleshooting.
    """
    started_at = datetime.now(UTC)
    job_name = f"do_all_{started_at.strftime('%Y-%m-%d_%H-%M-%S')}"

    try:
        seed = SeedHandler(session=session)
        edh = ExcelDataHandler(session=session)

        edh.validate_excel_input()

        seed.seed_roles()
        seed.seed_sites()
        seed.seed_prefix_pool_types()
        seed.seed_prefix_pools()
        seed.seed_resource_pools()

        edh.create_actions_blob_for_devices_loaded_from_excel()
        edh.create_actions_blob_for_cables_loaded_from_excel()
        edh.create_actions_blob_for_dist_devices_loaded_from_excel()
        edh.create_actions_blob_for_pe_ring_cables_from_half_open_rings()
        edh.create_actions_blob_for_ces_loaded_from_excel()
        edh.execute_job(job_name=job_name)

        edh.compute_services()

        # Assign sticky management IPs to every CE (needs the delegated /28s of
        # the CE-management VPRN that compute_services() has just allocated).
        # Read-only w.r.t. device config — it only populates the CeMgmtAddress
        # table that backs the report_ce_mgmt tab.
        allocate_ce_mgmt_addresses(session=session)

        snapshot_latest()
        write_report_tabs(session=session)
    except Exception:
        snapshot_failed()
        raise

    return PipelineResult(
        job_name=job_name,
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )
