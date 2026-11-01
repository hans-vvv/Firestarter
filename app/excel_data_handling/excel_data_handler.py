"""Parses the input Excel workbook into validated actions blobs consumed by the job executor."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.db import Base, engine
from app.domain.file_locations import TOPOLOGY_EXCEL_LOC
from app.domain.half_open_ring import terminating_devices
from app.logging.logger import archive_log, setup_logging
from app.models import Job, User, UserRole
from app.repositories import (
    get_job_by_name,
    get_role_by_name,
    get_site_by_name,
)
from app.services.job_handling.job_executor import JobExecutor
from app.utils import load_sheet, require
from app.validation.excel_input_checks import validate_excel_inputs


class ExcelDataHandler:
    """
    Orchestrates ingestion, validation, and transformation of Excel-based
    topology input into executable job actions.

    This class is responsible for:
    - Loading structured data from predefined Excel sheets.
    - Validating input data consistency and integrity.
    - Translating rows into normalized "action steps" (actions_blob)
      consumable by the JobExecutor.
    - Managing execution of those actions as a Job within the database.

    Core responsibilities:
    ----------------------
    1. Input validation:
        - Reads multiple sheets (Devices, Cables, DistDevices,
          HalfOpenRings, Site).
        - Delegates validation to `validate_excel_inputs`.
        - Fails fast if inconsistencies are detected.

    2. Action generation:
        - Converts Excel rows into structured action dictionaries.
        - Supports multiple domains:
            * Devices (core/RR, PE, CE)
            * Cables (including derived ring topology)
            * PE pairs and CE attachments
        - Accumulates actions in `actions_blob`.

    3. Topology derivation:
        - Builds ring relationships from HalfOpenRings.
        - Infers inter-device cabling for PE chains.

    4. Job execution:
        - Persists actions as a Job entity (if not already present).
        - Executes actions via JobExecutor.
        - Clears internal state after execution.

    Attributes:
        session:
            Active database session used for lookups and persistence.

        wb_name:
            Path to the Excel workbook containing input data.

        actions_blob:
            In-memory list of action dictionaries representing the
            desired topology changes.

    Notes:
        - This class assumes strict schema and naming conventions
          in the Excel workbook.
        - Database entities (Site, Role, Job) must exist or be seeded
          prior to action generation.
        - The class is stateful: `actions_blob` is built incrementally
          and consumed during job execution.
    """

    WB_NAME = TOPOLOGY_EXCEL_LOC.path

    def __init__(self, session: Session, wb_name: str | Path = WB_NAME):

        self.session = session
        self.wb_name = wb_name
        self.actions_blob: list[dict[str, Any]] = []

    def validate_excel_input(self) -> None:
        """
        Load required Excel sheets and validate their contents.

        This method reads predefined sheets from the configured workbook,
        runs validation checks, and reports any detected issues.

        Workflow:
        - Load sheets: Devices, Cables, DistDevices, HalfOpenRings, Site.
        - Pass loaded DataFrames to the validation routine.
        - If validation errors are found:
            - Print up to the first 50 errors in a compact format.
            - Raise a ValueError summarizing the total number of errors.

        Raises:
            ValueError: If one or more validation errors are detected.
        """

        df_devices = load_sheet(sheet_name="Devices", wb_name=self.wb_name)
        df_cables = load_sheet(sheet_name="Cables", wb_name=self.wb_name)

        df_dist = load_sheet(sheet_name="DistDevices", wb_name=self.wb_name)
        df_rings = load_sheet(sheet_name="HalfOpenRings", wb_name=self.wb_name)

        df_sites = load_sheet(sheet_name="Site", wb_name=self.wb_name)

        errors = validate_excel_inputs(
            df_devices=df_devices,
            df_dist=df_dist,
            df_cables=df_cables,
            df_half_open_rings=df_rings,
            df_sites=df_sites,
        )

        if errors:
            # keep formatting minimal
            for e in errors[:50]:
                print(f"{e.sheet} r{e.row} c{e.column}: {e.message}")
            raise ValueError(f"Excel input validation failed ({len(errors)} errors)")

    def create_actions_blob_for_dist_devices_loaded_from_excel(self) -> None:
        """
        Reads "DistDevices" Excel tab and create actions step
        for each row
        """

        df = load_sheet(sheet_name="DistDevices", wb_name=self.wb_name)

        ring_map = self._build_ring_map_from_half_open_rings()

        for row in df.to_dict("records"):
            device_name = row["DeviceName"]
            role_name = row["RoleName"]
            site_name = row["SiteName"]
            model_name = row["ModelName"]
            tenant = row["Tenant"]

            if "," not in device_name:
                self._create_actions_step_add_device(
                    site_name=site_name,
                    role_name=role_name,
                    hostname=device_name,
                    model_name=model_name,
                    tenant=tenant,
                    ring=ring_map.get("model_name"),
                )
            else:
                dev_a, dev_b = [d.strip() for d in device_name.split(",")]
                self._create_actions_step_add_pe_pair(
                    site_name=site_name,
                    hostname_a=dev_a,
                    hostname_b=dev_b,
                    role_name=role_name,
                    model_name=model_name,
                    tenant=tenant,
                    ring=ring_map.get("dev_a"),
                )

    def create_actions_blob_for_devices_loaded_from_excel(self) -> None:
        """
        Reads "Devices" Excel tab and create actions step
        for each row
        """

        df = load_sheet(sheet_name="Devices", wb_name=self.wb_name)

        for row in df.to_dict("records"):
            self._create_actions_step_add_device(
                hostname=row["DeviceName"],
                role_name=row["DeviceRole"],
                site_name=row["Site"],
                model_name=row["Model"],
                tenant=row["Tenant"],
            )

    def create_actions_blob_for_ces_loaded_from_excel(self) -> None:
        """
        Reads "CEs" Excel tab and create actions step
        for each row
        """

        df = load_sheet(sheet_name="CEs", wb_name=self.wb_name)

        for row in df.to_dict("records"):
            value: str = row.get("ConnectedPE", "")
            connected_pe = value.strip() if pd.notna(value) else None

            self._create_actions_step_attach_ce(
                ce_name=row["CEname"],
                ce_role_name=row["CERole"],
                site_name=row["SiteName"],
                model_name=row["ModelName"],
                connected_pe=connected_pe,
            )

    def create_actions_blob_for_cables_loaded_from_excel(self) -> None:
        """
        Reads "Cables" Excel tab and create actions step
        for each row
        """

        df = load_sheet(sheet_name="Cables", wb_name=self.wb_name)

        for row in df.to_dict("records"):
            self._create_actions_step_add_cable(
                dev_a_name=row["Device_a"],
                dev_b_name=row["Device_b"],
                iface_a_name=row["Iface_a"],
                iface_b_name=row["Iface_b"],
            )

    def create_actions_blob_for_pe_ring_cables_from_half_open_rings(self) -> None:
        """
        Emit add_cable actions for PE half-open rings based on the
        HalfOpenRings sheet.

        Topology model
        --------------
        Each row represents one half-open ring between two core routers:

            coreX.<Termination_site_a>  ...PE chain...  coreY.<Termination_site_b>

        The PE chain is defined by the remaining non-empty cells in the row,
        in sheet column order.

        Cell semantics
        --------------
        - A cell with one hostname, e.g. "pe1.site":
            entry = exit = "pe1.site"

        - A cell with two hostnames, e.g. "pe1.site,pe2.site":
            entry = "pe1.site"
            exit  = "pe2.site"

        The two hostnames in the same cell are assumed to already be cabled
        together internally, so this method must only emit the *external*
        inter-site cables:
            previous_exit -> next_entry

        Emitted cables
        --------------
        For each row, this method creates:
        - core_a -> entry(first site)
        - exit(site_i) -> entry(site_i+1)   for each adjacent pair
        - exit(last site) -> core_b

        Special case
        ------------
        If Termination_site_a == Termination_site_b, the far end is assumed to be
        the second core router on the same site, and core_b becomes:
            core2.<Termination_site_b>
        instead of:
            core1.<Termination_site_b>
        (see ``app.domain.half_open_ring.terminating_devices``).
        """
        df = load_sheet(sheet_name="HalfOpenRings", wb_name=self.wb_name)

        for row in df.to_dict("records"):
            site_a = str(row.get("Termination_site_a") or "").strip()
            site_b = str(row.get("Termination_site_b") or "").strip()

            if not site_a or not site_b:
                continue

            core_a, core_b = terminating_devices(site_a=site_a, site_b=site_b)

            chain: list[tuple[str, str]] = []

            for col, val in row.items():
                if col in ("Termination_site_a", "Termination_site_b") or pd.isna(val):
                    continue

                cell = str(val).strip()
                if not cell:
                    continue

                parts = [p.strip() for p in cell.split(",") if p.strip()]

                if len(parts) == 1:
                    entry = exit_ = parts[0]
                else:
                    entry = parts[0]
                    exit_ = parts[1]

                chain.append((entry, exit_))

            if not chain:
                continue

            self._create_actions_step_add_cable(
                dev_a_name=core_a,
                dev_b_name=chain[0][0],
            )

            for i in range(len(chain) - 1):
                self._create_actions_step_add_cable(
                    dev_a_name=chain[i][1],
                    dev_b_name=chain[i + 1][0],
                )

            self._create_actions_step_add_cable(
                dev_a_name=chain[-1][1],
                dev_b_name=core_b,
            )

    def _create_actions_step_add_device(
        self,
        *,
        site_name: str,
        role_name: str,
        hostname: str,
        model_name: str,
        tenant: str,
        ring: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates actions step for Job Handlers
        """

        site = require(
            get_site_by_name(self.session, site_name), f"No dB record exists for {site_name}"
        )
        role = require(
            get_role_by_name(self.session, role_name), f"No dB record exists for {role_name}"
        )

        actions_step = {
            "action": "add_device",
            "params": {
                "hostname": hostname,
                "role": role.name,
                "site": site.name,
                "model_name": model_name,
                "tenant": tenant,
                "ring": ring,
            },
        }
        self.actions_blob.append(actions_step)
        return actions_step

    def _create_actions_step_attach_ce(
        self,
        *,
        ce_name: str,
        ce_role_name: str,
        site_name: str,
        model_name: str,
        connected_pe: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates actions step for Job Handlers
        """

        site = require(
            get_site_by_name(self.session, site_name), f"No dB record exists for {site_name}"
        )
        ce_role = require(
            get_role_by_name(self.session, ce_role_name), f"No dB record exists for {ce_role_name}"
        )

        actions_step = {
            "action": "attach_ce",
            "params": {
                "ce_name": ce_name,
                "ce_role_name": ce_role.name,
                "site_name": site.name,
                "ce_model_name": model_name,
                "connected_pe": connected_pe,
            },
        }
        self.actions_blob.append(actions_step)
        return actions_step

    def _create_actions_step_add_pe_pair(
        self,
        *,
        site_name: str,
        hostname_a: str,
        hostname_b: str,
        role_name: str,
        model_name: str,
        tenant: str,
        ring: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates actions step for Job Handlers
        """
        site = require(
            get_site_by_name(self.session, site_name), f"No dB record exists for {site_name}"
        )
        role = require(
            get_role_by_name(self.session, role_name), f"No dB record exists for {role_name}"
        )

        params = {
            "role": role.name,
            "site": site.name,
            "hostname_a": hostname_a,
            "hostname_b": hostname_b,
            "model_name": model_name,
            "tenant": tenant,
            "ring": ring,
        }

        actions_step = {"action": "add_pe_pair", "params": params}
        self.actions_blob.append(actions_step)
        return actions_step

    def _create_actions_step_add_cable(
        self,
        *,
        dev_a_name: str,
        dev_b_name: str,
        iface_a_name: str | None = None,
        iface_b_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Creates actions step for Job Handlers
        """

        actions_step = {
            "action": "add_cable",
            "params": {
                "device_a_name": self._clean_scalar(dev_a_name),
                "device_b_name": self._clean_scalar(dev_b_name),
                "iface_a_name": self._clean_scalar(iface_a_name),
                "iface_b_name": self._clean_scalar(iface_b_name),
            },
        }

        self.actions_blob.append(actions_step)
        return actions_step

    def _build_ring_map_from_half_open_rings(self) -> dict[str, str]:
        """
        Build a mapping of PE device hostname -> ring identifier
        based on the HalfOpenRings Excel sheet tab.

        Each row in the sheet represents one half-open ring:
        - 'Termination_site_a' and 'Termination_site_b' define the two
            core sites forming the ring endpoints.
        - All other non-empty cells in the row contain PE device
            hostnames (either single hostname or comma-separated pair).

        The ring identifier is constructed as:
            "<siteA>:<siteB>"
        where the two site names are sorted lexicographically to ensure
        a stable and deterministic key.

        Returns:
            dict[str, str]:
                Mapping of device hostname -> ring string.

        Example:
            If a row contains:
                Termination_site_a = "dgrn-1a"
                Termination_site_b = "hlm-a2a"
                site_name_1 = "pe1.site_name_1,pe2.site_name_1"

            The resulting mapping will include:
                {
                    "pe1.site_name_1": "dgrn-1a:hlm-a2a",
                    "pe2.site_name_1": "dgrn-1a:hlm-a2a",
                }
        """
        df = load_sheet(sheet_name="HalfOpenRings", wb_name=self.wb_name)
        ring_map: dict[str, str] = {}

        for row in df.to_dict("records"):
            # Read termination sites directly
            site_a = str(row.get("Termination_site_a") or "")
            site_b = str(row.get("Termination_site_b") or "")

            if not site_a or not site_b:
                continue

            ring = ":".join(sorted([site_a, site_b]))

            # Collect all device cells in the row
            for col, val in row.items():
                if col in ("Termination_site_a", "Termination_site_b"):
                    continue
                if pd.isna(val):
                    continue

                cell = str(val).strip()
                if not cell:
                    continue

                # Cell may contain "pe1.site,pe2.site"
                for hostname in [x.strip() for x in cell.split(",") if x.strip()]:
                    ring_map[hostname] = ring

        return ring_map

    def _clean_scalar(self, x: Any) -> Any | None:
        """
        Normalize a scalar value by converting empty or invalid inputs to None.

        Rules:
        - None is returned as None.
        - NaN (float) is treated as missing and converted to None.
        - Strings are stripped of leading/trailing whitespace:
            - If the result is an empty string, return None.
            - Otherwise, return the cleaned string.
        - All other values are returned unchanged.

        Args:
            x: The input scalar value to clean.

        Returns:
            The cleaned value, or None if the input is considered empty or invalid.
        """
        if x is None:
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        if isinstance(x, str):
            x = x.strip()
            return x or None
        return x

    @staticmethod
    def wipe_db() -> None:
        """Drop and recreate the rebuildable tables, archive the log, reinitialise logging.

        Everything the pipeline computes — topology, allocations, services — is
        derived from the Excel workbook and the YAML definitions, so dropping it
        costs nothing: the next run reproduces it exactly. Dashboard accounts are
        not derived from anything. They are typed in by an operator, the password
        is stored only as a hash, and no input file can regenerate them.

        So the auth tables are excluded from the drop. Before this, a rebuild on a
        live instance destroyed every account; the web bootstrap then found an
        empty ``user`` table and re-seeded ``admin``/``changeme``, silently
        resetting the deployment to a well-known credential. Nothing in the
        compliance diff can catch that — accounts do not appear in rendered
        configs.

        ``create_all`` remains unconditional and is idempotent, so a genuinely
        fresh database still gets the auth tables created here.
        """
        archive_log()

        # Derived from the models rather than hardcoded names, so renaming a
        # table cannot silently drop it back into the wipe set.
        auth_tables = {User.__tablename__, UserRole.__tablename__}
        rebuildable = [t for t in Base.metadata.sorted_tables if t.name not in auth_tables]

        Base.metadata.drop_all(bind=engine, tables=rebuildable)
        Base.metadata.create_all(bind=engine)
        setup_logging()
        import logging

        logging.getLogger("app").info("Database wiped — new run started")

    def execute_job(self, *, job_name: str) -> bool:
        """Create (or refresh) a Job record from the current actions_blob and execute it.

        Only builds topology (devices, cables, CEs).  Service computation is
        intentionally excluded — call compute_services() explicitly once the
        topology is complete.

        On re-runs the existing Job row is reused — but its ``actions_blob``
        is refreshed from the freshly built ``self.actions_blob`` so any new
        Excel rows (a brand-new PE device, a removed device, an extra
        cable) actually make it into the executor. Without the refresh the
        executor replays the *original* plan from the first run, every
        per-action idempotency check fires ``"exists"``, and the new rows
        are silently dropped.

        ``flag_modified`` is required because ``Job.actions_blob`` is a
        plain ``JSON`` column (not wrapped in ``MutableDict.as_mutable``):
        the reassignment alone may not be detected by SQLAlchemy as dirty.
        """
        job = get_job_by_name(self.session, job_name)
        if job is None:
            job = Job(name=job_name, actions_blob=self.actions_blob)
            self.session.add(job)
        else:
            job.actions_blob = list(self.actions_blob)
            flag_modified(job, "actions_blob")
        self.session.flush()

        executor = JobExecutor(session=self.session)
        topology_changed = executor.execute(job)
        self.actions_blob.clear()

        return topology_changed

    def compute_services(self) -> None:
        """Run the service orchestrator as a standalone step.

        Must be called after ``execute_job`` so the topology the feature
        handlers read (devices, cables, CE attachments) is complete.
        """
        executor = JobExecutor(session=self.session)
        executor.service_orchestrator.submit()
