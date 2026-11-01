from __future__ import annotations

"""Web-layer utilities.

All functions that Flask routes need beyond pure HTTP handling live here:
session lifecycle, ORM-to-dict serialisation, and thin wrappers that wire
the business-logic layer to the web layer without coupling routes to
SQLAlchemy or service internals.
"""

import logging
import os
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.automation import backup
from app.automation.backup import FetchResult
from app.automation.deploy import DeployResult, DeployStep, DeviceExchange
from app.automation.inventory import build_inventory
from app.automation.simulated_devices import run_simulated_backup
from app.compliance.remediation import DeviceRemediation, project_remediation
from app.compliance.reporter import ComplianceReporter
from app.compliance.runner import ComplianceRunner, ComplianceRunSummary
from app.data_bundle.core import build_bundle
from app.data_bundle.spec import resolve_bundle_files
from app.domain.file_locations import (
    TOPOLOGY_EXCEL_LOC,
    data_root,
)
from app.excel_data_handling.reports import build_report_ce_mgmt
from app.models import ServiceInstance
from app.pipeline import PipelineResult, run_pipeline
from app.printing.printer import Printer
from app.repositories.device import (
    get_all_devices,
    get_device_by_hostname,
    get_devices_with_mgmt_address,
)
from app.repositories.device_credential import (
    get_device_credential,
    set_device_credential,
)
from app.repositories.job import (
    get_all_jobs,
    get_job_by_id,
)
from app.repositories.user import get_all_user_roles, get_all_users
from app.services.job_handling.job_executor import JobExecutor
from app.utils import db_session, load_sheet, require
from app.validation.excel_input_checks import ValidationError, validate_excel_inputs
from app.web import accounts
from app.web.push_guard import (
    DIGEST_MISMATCH,
    command_digest,
    digest_matches,
)

# ---------------------------------------------------------------------------
# Input Excel workbook — download / validate-before-replace.
# ---------------------------------------------------------------------------


def topology_excel_path() -> Path:
    """Absolute path to the input workbook (``TOPOLOGY_EXCEL_LOC``).

    Resolved under the live data root rather than the relative location string so
    it is correct whatever directory the Flask process was started from.
    """
    return TOPOLOGY_EXCEL_LOC.path


@dataclass(frozen=True)
class WorkbookValidation:
    """Outcome of validating an uploaded workbook before it replaces the current file.

    Separates the two ways a workbook can be rejected so the web layer can present
    each one to the operator instead of swallowing it:

    - ``errors`` — the structured, per-cell validation failures the checks return
      (including a single "could not read workbook" entry for a corrupt file or a
      missing sheet, which is a clean, expected failure).
    - ``crash_traceback`` — the formatted stack trace captured when a check raised
      an *unexpected* exception (a malformed sheet the validators did not
      anticipate) rather than returning a clean error. Without this the route would
      500 and the operator would never see why the upload was refused.

    A workbook is accepted only when both are empty/None (:pyattr:`ok`).
    """

    errors: list[ValidationError]
    crash_traceback: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.crash_traceback is None


def validate_excel_workbook(*, wb_path: str | Path) -> WorkbookValidation:
    """Run the Excel input checks against the workbook at *wb_path*.

    Returns a :class:`WorkbookValidation` so the caller can present both expected
    validation errors and an unexpected crash. The same sheets the ingestion
    pipeline validates (see ``ExcelDataHandler.validate_excel_input``) are loaded
    here. A workbook that cannot be read at all — corrupt file, missing required
    sheet — is reported as a single structured error rather than raised. A check
    that raises an unexpected exception is captured as a traceback rather than
    allowed to propagate, so the web layer can surface it the same way it surfaces
    content errors.
    """
    try:
        df_devices = load_sheet(sheet_name="Devices", wb_name=wb_path)
        df_cables = load_sheet(sheet_name="Cables", wb_name=wb_path)
        df_dist = load_sheet(sheet_name="DistDevices", wb_name=wb_path)
        df_rings = load_sheet(sheet_name="HalfOpenRings", wb_name=wb_path)
        df_sites = load_sheet(sheet_name="Site", wb_name=wb_path)
    except Exception as exc:
        return WorkbookValidation(
            errors=[
                ValidationError(
                    sheet="(workbook)",
                    row=None,
                    column=None,
                    message=f"Could not read workbook: {exc}",
                )
            ]
        )

    try:
        errors = validate_excel_inputs(
            df_devices=df_devices,
            df_dist=df_dist,
            df_cables=df_cables,
            df_half_open_rings=df_rings,
            df_sites=df_sites,
        )
    except Exception:
        # An input the validators did not anticipate made a check raise instead of
        # returning a clean error. Capture the trace so the operator sees it in the
        # modal rather than getting a bare 500.
        return WorkbookValidation(errors=[], crash_traceback=traceback.format_exc())

    return WorkbookValidation(errors=errors)


# ---------------------------------------------------------------------------
# Production data bundle — download all environment-specific state (database,
# workbook, production YAML, compliance reference/ignore files) as one ZIP.
# ---------------------------------------------------------------------------


def data_bundle_environment() -> str:
    """This server's environment label (``FIRESTARTER_ENV``), default ``unknown``.

    Recorded in a downloaded bundle's manifest so whoever unpacks it can see which
    environment the snapshot came from. Left ``unknown`` on an unlabelled server.
    """
    return os.getenv("FIRESTARTER_ENV", "unknown")


def data_bundle_preview() -> dict:
    """Summarise what a bundle would contain, for the download page.

    Returns the environment label and the per-file list (repo-relative path +
    size in KB), plus a total, so the operator can see exactly what they are
    about to export before clicking download. Reads nothing but file sizes.
    """
    root = data_root()
    files = resolve_bundle_files(data_root=root)
    items = [
        {
            "path": rel.as_posix(),
            "size_kb": f"{(root / rel).stat().st_size / 1024:.1f}",
        }
        for rel in files
    ]
    return {
        "environment": data_bundle_environment(),
        "files": items,
        "count": len(items),
    }


def build_data_bundle() -> bytes:
    """Build the production-data ZIP bundle for this environment and return it."""
    return build_bundle(data_root=data_root(), environment=data_bundle_environment())


# ---------------------------------------------------------------------------
# Topology reads — ORM objects are serialised to plain dicts before the
# session closes so Jinja2 templates never trigger lazy loads.
# ---------------------------------------------------------------------------


def list_devices() -> list[dict]:
    """Return all devices as plain dicts, ordered by id."""
    with db_session() as session:
        devices = get_all_devices(session)
        return [_device_to_dict(d) for d in devices]


def list_latest_backups() -> tuple[list[dict], str | None]:
    """Return every device augmented with its latest-backup state, plus the run stamp.

    Left-joins :func:`list_devices` with the most recent backup run
    (:func:`app.automation.backup.load_results`), so the result is device-centric:
    the same rows the Device list shows — carrying the same ``role``/``site``/
    ``model_name``/``status`` fields the filters key on — each with a
    ``backup_status`` of ``"ok"`` (config on disk), ``"failed"`` (the run recorded
    an error) or ``"missing"`` (the run has no record of this device, or no run has
    happened). ``backup_error`` carries the recorded error for the failed case.

    The second element is the run's collection timestamp, or ``None`` when no run
    has been recorded. ``backup.BACKUPS_DIR`` is read live (not captured in a
    default argument) so tests can repoint it at a single monkeypatch point.
    """
    results = backup.load_results(backups_dir=backup.BACKUPS_DIR)
    rows: list[dict] = []
    for device in list_devices():
        result = results.get(device["hostname"])
        if result is None:
            status, error = "missing", None
        elif result.ok and result.raw is not None:
            status, error = "ok", None
        else:
            status, error = "failed", result.error
        rows.append({**device, "backup_status": status, "backup_error": error})
    return rows, backup.read_timestamp(backups_dir=backup.BACKUPS_DIR)


def latest_backup(*, hostname: str) -> FetchResult | None:
    """Return one device's latest stored backup outcome, or ``None`` if unknown.

    Thin wrapper over :func:`app.automation.backup.read_backup` that reads
    ``backup.BACKUPS_DIR`` live, for the same test-monkeypatch reason as
    :func:`list_latest_backups`.
    """
    return backup.read_backup(hostname, backups_dir=backup.BACKUPS_DIR)


def get_device(*, hostname: str) -> dict | None:
    """Return a single device as a plain dict, or None if not found."""
    with db_session() as session:
        device = get_device_by_hostname(session, hostname)
        if device is None:
            return None
        return _device_to_dict(device)


def list_jobs() -> list[dict]:
    """Return all jobs as plain dicts, ordered by id."""
    with db_session() as session:
        jobs = get_all_jobs(session)
        return [_job_to_dict(j) for j in jobs]


def get_job(*, job_id: int) -> dict | None:
    """Return a single job as a plain dict, or None if not found."""
    with db_session() as session:
        job = get_job_by_id(session, job_id)
        if job is None:
            return None
        return _job_to_dict(job)


# ---------------------------------------------------------------------------
# Config rendering
# ---------------------------------------------------------------------------


def render_device(*, hostname: str) -> str:
    """Render and return the configuration for a single device."""
    with db_session() as session:
        return Printer(session=session).render_device(hostname=hostname)


def render_all() -> dict[str, str]:
    """Render configurations for every device that has computed services."""
    with db_session() as session:
        return Printer(session=session).print_all()


def render_all_to_disk() -> dict[str, str]:
    """Render all configs and write them to ``app/artifacts/latest/``."""
    with db_session() as session:
        return Printer(session=session).write_to_disk()


# ---------------------------------------------------------------------------
# Per-service config snippets
# ---------------------------------------------------------------------------


# Service context keys whose named objects can be selected individually in the
# snippet picker. The vprn service writes under "vprn", evpn_vpls under
# "evpn_vpls". Objects live at ctx[key]["variant"][variant][object_name].
_DRILLDOWN_CTX_KEYS = ("vprn", "evpn_vpls")


def list_device_service_snippet_items(*, hostname: str) -> list[dict]:
    """Return the selectable VPRN/VPLS objects for ``hostname``, grouped by instance.

    One entry per VPRN/VPLS ServiceInstance whose computed intent touches the
    device, each carrying an ``objects`` list — the named VPRNs / VPLS objects
    inside it, individually selectable. Services with no named VPRN/VPLS objects
    (isis, sr, bgp, evpn_esi) are intentionally omitted: the snippet tool is
    scoped to VPRN and VPLS.

    Object tokens are the wire format the picker posts back and
    :func:`render_service_snippet` parses:
    ``obj|{id}|{ctx_key}|{variant}|{object_name}``.

    Sorted deterministically (service, tenant, variant; objects by name).
    """
    with db_session() as session:
        instances = (
            session.execute(select(ServiceInstance).where(ServiceInstance.computed.is_not({})))
            .scalars()
            .all()
        )
        return _build_snippet_items(list(instances), hostname=hostname)


def _build_snippet_items(instances: list[ServiceInstance], *, hostname: str) -> list[dict]:
    """Filter ``instances`` to those exposing VPRN/VPLS objects on ``hostname``.

    Pure (no DB) so it is unit-testable. An instance is included only when it
    has at least one named VPRN/VPLS object on the device, which automatically
    drops the underlay/L2 whole-instance services (isis/sr/bgp/evpn_esi).
    """
    items: list[dict] = []
    for inst in instances:
        dev = (inst.computed or {}).get(hostname)
        if not dev:
            continue
        objects = _enumerate_objects(inst_id=inst.id, dev_ctx=dev)
        if not objects:
            continue
        items.append(
            {
                "id": inst.id,
                "svc_name": inst.svc_name,
                "tenant": inst.tenant,
                "variant": inst.variant,
                "objects": objects,
            }
        )
    items.sort(key=lambda d: (d["svc_name"], d["tenant"], d["variant"]))
    return items


def _enumerate_objects(*, inst_id: int, dev_ctx: dict) -> list[dict]:
    """Return the individually-selectable named objects in a device context.

    Inspects only the drill-down context keys (vprn / evpn_vpls); other
    services contribute no objects and stay whole-instance selectable.
    """
    objects: list[dict] = []
    for ctx_key in _DRILLDOWN_CTX_KEYS:
        block = dev_ctx.get(ctx_key)
        if not isinstance(block, dict):
            continue
        for variant_name, objs in block.get("variant", {}).items():
            if not isinstance(objs, dict):
                continue
            for object_name in objs:
                objects.append(
                    {
                        "ctx_key": ctx_key,
                        "variant_name": variant_name,
                        "object_name": object_name,
                        "token": f"obj|{inst_id}|{ctx_key}|{variant_name}|{object_name}",
                    }
                )
    objects.sort(key=lambda d: (d["ctx_key"], d["variant_name"], d["object_name"]))
    return objects


def render_service_snippet(*, hostname: str, selection: list[str]) -> list[str]:
    """Return the config lines the selected snippet leaves contribute.

    ``selection`` is a list of tokens (see
    :func:`list_device_service_snippet_items`): whole-instance and/or single-
    object tokens. They are parsed into the two exclusion structures the Printer
    understands, then the marginal-contribution diff is taken — render
    ``hostname`` with everything, render again with the selected leaves excluded,
    return the lines present only in the full render.

    SR-OS MD-CLI lines are fully-qualified absolute paths, so the set diff is
    order-insensitive and safe (the same pattern the compliance/migration
    tooling uses). A fresh ``Printer`` is built per render because
    ``_collect_services_computed`` is a ``@cached_property``.

    An empty or fully unparseable ``selection`` yields an empty list without
    rendering.
    """
    exclude_instance_ids: set[int] = set()
    exclude_objects: dict[int, list[tuple[str, str, str]]] = {}
    for token in selection:
        parts = token.split("|")
        if parts[0] == "inst" and len(parts) == 2 and parts[1].isdigit():
            exclude_instance_ids.add(int(parts[1]))
        elif parts[0] == "obj" and len(parts) >= 5 and parts[1].isdigit():
            ctx_key, variant_name = parts[2], parts[3]
            object_name = "|".join(parts[4:])  # names may (rarely) contain '|'
            exclude_objects.setdefault(int(parts[1]), []).append(
                (ctx_key, variant_name, object_name)
            )

    if not exclude_instance_ids and not exclude_objects:
        return []

    with db_session() as session:
        full = Printer(session=session).render_device(hostname=hostname)
        without = Printer(
            session=session,
            exclude_instance_ids=exclude_instance_ids,
            exclude_objects=exclude_objects,
        ).render_device(hostname=hostname)
    return sorted(set(full.splitlines()) - set(without.splitlines()))


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


def submit_job(*, job_id: int) -> bool:
    """Execute a pending job by its primary key.

    Returns True if the job caused topology changes, False otherwise.
    Raises ValueError if no job with job_id exists.
    """
    with db_session() as session:
        job = require(
            get_job_by_id(session, job_id),
            f"No job found with id={job_id}",
        )
        executor = JobExecutor(session=session)
        return executor.execute(job)


# ---------------------------------------------------------------------------
# Data pipeline (full ingestion + service computation)
# ---------------------------------------------------------------------------


@dataclass
class PipelineLastRun:
    """Cached outcome of the most recent dashboard-triggered pipeline run.

    ``status`` is ``None`` until the pipeline has run at least once this process,
    then ``"ok"`` or ``"failed"``. On failure ``traceback`` holds the formatted
    stack trace so the dashboard can present it.
    """

    status: str | None = None
    job_name: str | None = None
    timestamp: datetime | None = None
    duration_seconds: float | None = None
    traceback: str | None = None


_pipeline_last_run = PipelineLastRun()


def get_pipeline_last_run() -> PipelineLastRun:
    """Return the cached result of the most recent pipeline run."""
    return _pipeline_last_run


def run_data_pipeline() -> PipelineResult:
    """Run the full data pipeline and record the outcome for the dashboard.

    Runs synchronously inside its own DB session (which rolls back on failure so
    a broken run never commits partial state). The outcome — success or the full
    traceback on failure — is stored in the module-level last-run cache and,
    on failure, logged via the ``app`` logger. The exception is re-raised so the
    calling route can present the error to the operator.
    """
    global _pipeline_last_run
    with db_session() as session:
        try:
            result = run_pipeline(session=session)
        except Exception:
            logging.getLogger("app").exception("Data pipeline run failed")
            _pipeline_last_run = PipelineLastRun(
                status="failed",
                timestamp=datetime.now(UTC),
                traceback=traceback.format_exc(),
            )
            raise

    _pipeline_last_run = PipelineLastRun(
        status="ok",
        job_name=result.job_name,
        timestamp=result.finished_at,
        duration_seconds=result.duration_seconds,
    )
    return result


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------


# Most recent compliance run, cached in-process so the dashboard can offer a
# detailed-report download without re-running the (expensive) render→fetch→diff
# pipeline. Mirrors the _pipeline_last_run / _isis_last_scan pattern above:
# a single shared slot, fine for this single-tenant internal dashboard. None
# until the first run this process; reset on restart.
_compliance_last_summary: ComplianceRunSummary | None = None


def run_compliance(*, hostnames: list[str] | None = None) -> ComplianceRunSummary:
    """Run the full compliance pipeline, cache the summary, and return it.

    The live side is whatever the most recent local backup run produced — in this
    demo, :func:`download_device_backups` over the simulated devices.
    """
    global _compliance_last_summary
    with db_session() as session:
        runner = ComplianceRunner(session=session, hostnames=hostnames)
        summary = runner.run()
    _compliance_last_summary = summary
    return summary


def get_last_compliance_summary() -> ComplianceRunSummary | None:
    """Return the most recent cached compliance summary, or None if never run."""
    return _compliance_last_summary


def compliance_report_text() -> str | None:
    """Format the most recent compliance run as a detailed text report.

    Returns the full human-readable report produced by
    :class:`ComplianceReporter` (summary table, per-device drift detail,
    failures, footer) for the last run cached in this process, or ``None``
    if no compliance run has happened yet. No ``report_dir`` is passed, so
    the report is built in memory and never written to disk.
    """
    summary = _compliance_last_summary
    if summary is None:
        return None
    return ComplianceReporter().report(summary=summary)


# ---------------------------------------------------------------------------
# Nornir device tasks
# ---------------------------------------------------------------------------


class DeviceCommandError(RuntimeError):
    """A device command could not be run, or came back failed."""


@dataclass(frozen=True)
class TaskOutcome:
    """Result of one optional pre-compliance task, for display."""

    name: str
    ok: bool
    detail: str


def rebuild_nornir_inventory() -> TaskOutcome:
    """Regenerate ``hosts.yaml``/``groups.yaml`` from the topology database.

    Needs no credentials — it reads the database, not the devices.

    Failures are returned rather than raised so one failed task does not
    abandon the rest of the operator's request; the caller renders the outcome
    either way.
    """
    try:
        with db_session() as session:
            result = build_inventory(session=session)
    except Exception as exc:
        logging.getLogger("app").exception("Nornir inventory rebuild failed")
        return TaskOutcome(name="Rebuild Nornir inventory", ok=False, detail=str(exc))

    return TaskOutcome(
        name="Rebuild Nornir inventory",
        ok=True,
        detail=f"{len(result.hosts)} active device(s), {len(result.roles)} role(s)",
    )


def download_device_backups() -> TaskOutcome:
    """Fetch every simulated device's configuration into ``backups/latest/``.

    The demo has no real devices, so this never opens an SSH session and needs no
    credentials: :func:`app.automation.simulated_devices.run_simulated_backup`
    renders each device, reshapes the result into a device dump and applies the
    drift file. The production code path (``app.automation.backup.run_backup`` over
    Nornir/Netmiko) stays in the tree for reference but is not reachable from here.

    A device the drift file marks unreachable is reported but does not fail the
    whole task: the run still refreshed every device that did respond, and that is
    what compliance compares against.
    """
    try:
        with db_session() as session:
            results = run_simulated_backup(session=session)
    except Exception as exc:
        logging.getLogger("app").exception("Simulated device backup run failed")
        return TaskOutcome(name="Fetch backups from simulated devices", ok=False, detail=str(exc))

    failed = sorted(hostname for hostname, r in results.items() if not r.ok)
    detail = f"{len(results) - len(failed)}/{len(results)} simulated device(s) fetched"
    if failed:
        detail += f" — unreachable: {', '.join(failed)}"
    return TaskOutcome(name="Fetch backups from simulated devices", ok=not failed, detail=detail)


# ---------------------------------------------------------------------------
# Users & authentication
# ---------------------------------------------------------------------------


def authenticate_user(*, username: str, password: str) -> dict | None:
    """Verify credentials; return the serialised user dict or None.

    Stamps ``last_login_at`` on success (committed on session exit).
    """
    with db_session() as session:
        user = accounts.authenticate(session, username=username, password=password)
        if user is None:
            return None
        return _user_to_dict(user)


def change_user_password(*, user_id: int, new_password: str) -> None:
    """Self-service password change; clears the forced-change flag."""
    with db_session() as session:
        accounts.change_password(session, user_id=user_id, new_password=new_password)


def list_users() -> list[dict]:
    """Return all users as plain dicts, ordered by username."""
    with db_session() as session:
        return [_user_to_dict(u) for u in get_all_users(session)]


def list_user_roles() -> list[str]:
    """Return all role names, ordered alphabetically."""
    with db_session() as session:
        return [r.name for r in get_all_user_roles(session)]


def create_user(*, username: str, role_name: str, password: str) -> None:
    """Admin action: create a user with a temporary (must-change) password."""
    with db_session() as session:
        accounts.create_user(session, username=username, role_name=role_name, password=password)


def set_user_role(*, user_id: int, role_name: str) -> None:
    """Admin action: change a user's role (refuses to demote the last admin)."""
    with db_session() as session:
        accounts.set_role(session, user_id=user_id, role_name=role_name)


def reset_user_password(*, user_id: int, temp_password: str) -> None:
    """Admin action: set a temporary password and force a change on next login."""
    with db_session() as session:
        accounts.reset_password(session, user_id=user_id, temp_password=temp_password)


def delete_user(*, user_id: int, acting_user_id: int) -> None:
    """Admin action: delete a user (refuses self-delete and last-admin delete)."""
    with db_session() as session:
        accounts.delete_user(session, user_id=user_id, acting_user_id=acting_user_id)


# ---------------------------------------------------------------------------
# Device credentials and management addresses
# ---------------------------------------------------------------------------


def device_admin_password_is_set() -> bool:
    """True once the production device password has been recorded."""
    with db_session() as session:
        return get_device_credential(session) is not None


def set_device_admin_password(*, password: str) -> None:
    """Record (or replace) the hash of the production device password."""
    with db_session() as session:
        set_device_credential(session, password=password)


def devices_with_mgmt_address() -> list[tuple[str, str]]:
    """``(hostname, address)`` for every device with a management IP."""
    with db_session() as session:
        return get_devices_with_mgmt_address(session)


def ce_mgmt_report_rows() -> list[dict]:
    """The ``report_ce_mgmt`` rows as plain dicts, one per CE.

    Thin facade over :func:`build_report_ce_mgmt` — same rows the Excel report
    tab holds (``pe1``/``port1``/``pe2``/``port2``, CIDR ``mgmt_ip``,
    ``vrrp_gateway``, ``ce_role``, …). Keeping the raw rows here means the
    report's link and subnet logic lives in exactly one place.
    """
    with db_session() as session:
        return build_report_ce_mgmt(session).to_dict(orient="records")


# The most services a single deploy may push at once. Selecting more than this
# in the picker is fine — it just widens what the snippet *shows*; the guard
# only bites when the operator asks to push them. Keeping the blast radius of one
# push small is a deliberate safety limit, not a technical ceiling.
MAX_DEPLOY_SERVICES = 2

# This demo never contacts a device. Every "push" path below runs the same
# server-side derivation and digest check the production code did, then stops
# here instead of opening a session — so the pages still show exactly what a
# push *would* commit, and nothing is ever sent.
PUSH_DISABLED_NOTICE = "Push to device is disabled in this demo."


def _push_disabled(*, hostname: str, lines: list[str]) -> DeployResult:
    """The stub outcome of a push: the lines that would have been sent, and nothing sent.

    Shaped as a :class:`DeployResult` so the existing result partials render it
    unchanged: the lines appear in the device log as one unsent exchange, the
    steps say where the run stopped, and ``ok`` stays ``False`` because nothing was
    committed.
    """
    return DeployResult(
        hostname=hostname,
        ok=False,
        error=PUSH_DISABLED_NOTICE,
        steps=[
            DeployStep(name="Render", ok=True, detail=f"{len(lines)} line(s) derived"),
            DeployStep(name="Push to device", ok=False, detail="disabled in this demo"),
        ],
        exchanges=[
            DeviceExchange(
                command="\n".join(lines), output="(not sent — " + PUSH_DISABLED_NOTICE + ")"
            )
        ],
    )


def deploy_service_snippet(
    *,
    hostname: str,
    selection: list[str],
    presented_digest: str,
) -> DeployResult:
    """Render the selected snippet for *hostname* and show what a push would commit.

    The configuration is re-derived from *selection* here, server-side, via
    :func:`render_service_snippet` — the browser posts back which services were
    chosen and the digest of the lines it was shown, never the config text. The
    result is **refused** (:data:`DIGEST_MISMATCH`) unless the re-rendered lines
    still match that digest, and :data:`MAX_DEPLOY_SERVICES` is enforced — the
    same guards the production push applied. The push itself is disabled in this
    demo: no device is contacted, and the lines come back in the result for display.
    """
    if not selection:
        return DeployResult(hostname=hostname, error="Select at least one service to deploy.")
    if len(selection) > MAX_DEPLOY_SERVICES:
        return DeployResult(
            hostname=hostname,
            error=(
                f"Select at most {MAX_DEPLOY_SERVICES} services to deploy at once "
                f"({len(selection)} selected)."
            ),
        )

    lines = render_service_snippet(hostname=hostname, selection=selection)
    if not lines:
        return DeployResult(
            hostname=hostname,
            error=(
                f"The selected services contribute no configuration to {hostname}; nothing to push."
            ),
        )
    if not digest_matches(lines=lines, presented_digest=presented_digest):
        return DeployResult(hostname=hostname, error=DIGEST_MISMATCH)

    return _push_disabled(hostname=hostname, lines=lines)


def last_remediation_candidates() -> list[DeviceRemediation]:
    """Project remediation candidates from the most recent compliance run.

    A pure read of the cached run — no device contact — so the dashboard's
    remediation view always matches the compliance table the operator just saw.
    Empty when no run has happened in this process.
    """
    summary = get_last_compliance_summary()
    if summary is None:
        return []
    with db_session() as session:
        return project_remediation(results=summary.results, session=session)


def _push_command(line: str) -> str:
    """Turn a normalised remediation line back into a runnable MD-CLI command.

    Remediation's additive lines come from ``DiffResult.only_in_rendered``, i.e. the
    *normalised* diff, and normalisation strips the leading ``/`` (see the
    normaliser). But the deploy engine sends every line from **inside** exclusive
    config mode (prompt ``[ex:/configure]``), where a bare ``configure ...`` is not a
    valid child element — SR OS answers ``MINOR: MGMT_CORE #2201: Unknown element -
    'configure'``. The leading ``/`` makes it an absolute-path command that runs from
    any context, which is exactly the form the Printer emits and the proven
    snippet-deploy path already sends. Restore it here.

    Idempotent: a line that already starts with ``/`` is returned unchanged. Applied
    only to the additive ``lines``; operator ``remediation_commands`` (e.g. ``delete ...``,
    which is context-relative and worked as authored on kit) bypass this and are
    pushed verbatim.
    """
    return line if line.startswith("/") else "/" + line


def remediation_push_lines(candidate: DeviceRemediation) -> list[str]:
    """The exact MD-CLI lines a remediation push commits for one device.

    **The single source** for both the page and the push, so what is shown equals
    what is sent. Additive render lines first — each with its leading ``/`` restored
    (see :func:`_push_command`) so it runs from inside config mode — then the delete
    rules' verbatim ``remediation_commands`` (their capture groups already substituted
    in the projection). One commit-confirmed transaction, so add and remove land
    together.
    """
    return [_push_command(line) for line in candidate.lines] + list(candidate.remediation_commands)


@dataclass(frozen=True)
class RemediationCandidateView:
    """One remediation candidate as the page presents it — the push-ready view.

    ``add_lines`` and ``remove_lines`` are the exact commands the push will send (the
    additive lines with their ``/`` restored, then the delete commands), split only
    for display (``+`` vs ``~``). ``digest`` fingerprints their concatenation — the
    same sequence :func:`remediation_push_lines` produces — so the per-device push can
    refuse if a re-derivation no longer matches what was shown.
    """

    hostname: str
    role_name: str
    eligible: bool
    blocked_reasons: tuple[str, ...]
    add_lines: list[str]
    remove_lines: list[str]
    digest: str

    @property
    def line_count(self) -> int:
        return len(self.add_lines) + len(self.remove_lines)


def remediation_candidate_views() -> list[RemediationCandidateView]:
    """Project the cached run's remediation candidates into push-ready views.

    A pure read of :func:`last_remediation_candidates`, adding to each the exact
    push-ready lines (slash restored) and their digest — so the page shows, byte for
    byte, what a push would commit, and carries the digest the push verifies.
    """
    views: list[RemediationCandidateView] = []
    for c in last_remediation_candidates():
        views.append(
            RemediationCandidateView(
                hostname=c.hostname,
                role_name=c.role_name,
                eligible=c.eligible,
                blocked_reasons=c.blocked_reasons,
                add_lines=[_push_command(line) for line in c.lines],
                remove_lines=list(c.remediation_commands),
                digest=command_digest(remediation_push_lines(c)),
            )
        )
    return views


def _pe_remediation_plan(candidates: list[DeviceRemediation]) -> dict[str, list[str]]:
    """``{hostname: push_lines}`` for every eligible pe candidate — the batch plan.

    The single source for both the batch preview digest and the batch push, so the
    two cannot disagree on which devices or which lines the batch covers.
    """
    return {
        c.hostname: remediation_push_lines(c)
        for c in candidates
        if c.eligible and c.role_name == PE_ROLE
    }


def _batch_canonical(lines_by_host: dict[str, list[str]]) -> list[str]:
    """A deterministic serialisation of a whole batch plan for digesting.

    Host-order independent (sorted) and exact per host, so the digest changes if any
    device joins/leaves the batch or any of its lines change.
    """
    return [host + "\n" + "\n".join(lines_by_host[host]) for host in sorted(lines_by_host)]


def remediation_batch_digest(lines_by_host: dict[str, list[str]]) -> str:
    """Digest over an entire pe remediation batch plan (see :func:`_batch_canonical`)."""
    return command_digest(_batch_canonical(lines_by_host))


def pe_batch_digest() -> str:
    """The batch digest the remediation page carries for the 'remediate all pes' push."""
    return remediation_batch_digest(_pe_remediation_plan(last_remediation_candidates()))


def remediate_device(*, hostname: str, presented_digest: str) -> DeployResult:
    """Show what remediating one device would push — without pushing it.

    The lines are re-derived here, server-side, via :func:`remediation_push_lines`
    from the cached compliance run and the current per-role spec — the same source the
    page rendered. The browser posts the hostname and the digest of the lines it
    showed; the result is **refused** (:data:`DIGEST_MISMATCH`) unless the
    re-derivation still matches that digest, and eligibility is re-checked. The push
    itself is disabled in this demo: no device is contacted.
    """
    match = next((c for c in last_remediation_candidates() if c.hostname == hostname), None)
    if match is None:
        return DeployResult(
            hostname=hostname,
            error=f"{hostname} has no remediation candidates in the last compliance run.",
        )
    if not match.eligible:
        return DeployResult(
            hostname=hostname,
            error=f"{hostname} is not eligible for remediation: "
            + "; ".join(match.blocked_reasons),
        )

    lines = remediation_push_lines(match)
    if not digest_matches(lines=lines, presented_digest=presented_digest):
        return DeployResult(hostname=hostname, error=DIGEST_MISMATCH)

    return _push_disabled(hostname=hostname, lines=lines)


PE_ROLE = "pe"
"""The ``Role.name`` of a PE device. The batch button targets this role, not a
hostname prefix (see the remediation spec convention)."""


@dataclass(frozen=True)
class BatchRemediationResult:
    """Outcome of a 'remediate all pes' batch.

    ``results`` is one :class:`DeployResult` per eligible device (empty when nothing
    was eligible). ``digest_mismatch`` is set — with ``results`` empty — when the batch
    plan re-derived at push time no longer matched the digest the page carried, so
    nothing was contacted.
    """

    results: list[DeployResult]
    digest_mismatch: bool = False


def remediate_pe_devices(*, presented_digest: str) -> BatchRemediationResult:
    """Show what a 'remediate all pes' batch would push — without pushing it.

    "pe" is the :data:`PE_ROLE` role, not a hostname prefix. The eligible set
    — and each device's lines — is re-derived here (via :func:`_pe_remediation_plan`)
    from the cached compliance run and the current per-role specs, the same source the
    page previewed, so the browser posts no hostnames and no config text. The whole plan
    is digested and the batch is **refused** (``digest_mismatch``) unless it still
    matches the digest the page carried. The push itself is disabled in this demo: one
    stub result per device comes back, and no device is contacted.
    """
    lines_by_host = _pe_remediation_plan(last_remediation_candidates())
    if not lines_by_host:
        return BatchRemediationResult(results=[])
    if not digest_matches(lines=_batch_canonical(lines_by_host), presented_digest=presented_digest):
        return BatchRemediationResult(results=[], digest_mismatch=True)

    return BatchRemediationResult(
        results=[
            _push_disabled(hostname=hostname, lines=lines)
            for hostname, lines in lines_by_host.items()
        ]
    )


# ---------------------------------------------------------------------------
# Private serialisers — must be called while the session is still open
# ---------------------------------------------------------------------------


def _user_to_dict(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role.name if user.role else None,
        "must_change_password": user.must_change_password,
        "created_at": _iso(user.created_at),
        "last_login_at": _iso(user.last_login_at),
    }


def filter_devices(
    devices: list[dict],
    *,
    hostname: str = "",
    role: str = "",
    site: str = "",
    model: str = "",
    status: str = "",
) -> list[dict]:
    """Return the subset of *devices* that match all active filter values.

    An empty string for any parameter means no filter on that field.
    ``hostname`` is a case-insensitive substring match; all other fields
    require an exact match against the serialised device dict values.
    """
    result = devices
    if hostname:
        result = [d for d in result if hostname.lower() in (d["hostname"] or "").lower()]
    if role:
        result = [d for d in result if d["role"] == role]
    if site:
        result = [d for d in result if d["site"] == site]
    if model:
        result = [d for d in result if d["model_name"] == model]
    if status:
        result = [d for d in result if d["status"] == status]
    return result


def _device_to_dict(device) -> dict:
    return {
        "id": device.id,
        "hostname": device.hostname,
        "status": device.status,
        "model_name": device.model_name,
        "lag_name": device.lag_name,
        "labels": dict(device.labels or {}),
        "role": device.role.name if device.role else None,
        "site": device.site.name if device.site else None,
    }


def _iso(dt) -> str | None:
    """Return ISO string or None."""
    return dt.isoformat() if dt else None


def _job_to_dict(job) -> dict:
    """Serialise a Job (Excel-driven topology build) for the dashboard."""
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "created_at": _iso(job.created_at),
        "executed_at": _iso(job.executed_at),
        "committed_at": _iso(job.committed_at),
    }
