"""Job handler for the 'attach_ce' action — attaches a CE to PE UNI port(s)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.job_handling.base import BaseActionJobHandler

if TYPE_CHECKING:
    from app.services.job_handling.job_executor import JobExecutor

from app.repositories import (
    get_device_by_hostname,
)
from app.utils import require


class AddCeJobHandler(BaseActionJobHandler):
    """
    Idempotent job handler for attaching a CE to PE(s).
    """

    def __init__(self, executor: JobExecutor):
        self.executor = executor

    def identity(self, step: dict) -> str:
        ce_name = step["params"]["ce_name"]
        return f"add_ce {ce_name}"

    def handle(self, step: dict):
        params: dict[str, Any] = require(step.get("params"), "params missing")

        site_name = require(params.get("site_name"), "site is required")
        ce_name = require(params.get("ce_name"), "ce_name is required")
        ce_role_name = require(params.get("ce_role_name"), "ce_role_name is required")
        ce_model_name = require(params.get("ce_model_name"), "ce_model_name is required")
        connected_pe = params.get("connected_pe")

        # ---------------------------------------------------------
        # IDEMPOTENCY CHECK
        # ---------------------------------------------------------

        # CE devices are in dB after initial attachment to PE(s)
        ce = get_device_by_hostname(self.executor.session, ce_name)
        if ce is not None:
            return {
                "ce_id": ce.id,
                "status": "exists",
            }

        ce = self.executor.ce_attachment_builder.attach_ce(
            site_name=site_name,
            ce_name=ce_name,
            ce_role_name=ce_role_name,
            ce_model_name=ce_model_name,
            pe_role_name="pe",  # Must come from caller: TODO
            connected_pe=connected_pe,
        )

        return {"ce_id": ce.id, "status": "attached", "topology_changed": True}
