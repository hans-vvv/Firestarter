"""Compliance remediation package.

Code lives in :mod:`app.compliance.remediation.engine`; the per-role
``<role>.yaml`` spec files live alongside it as gitignored environment data
(mirroring ``app/compliance/ignore/``). The public API is re-exported here so
callers import from ``app.compliance.remediation`` directly.
"""

from __future__ import annotations

from app.compliance.remediation.engine import (
    BASE_REMEDIATION_FILE,
    PLACEHOLDER_PATTERN,
    REMEDIATION_DIR,
    DeviceRemediation,
    RemediationSpec,
    command_placeholders,
    evaluate_conditions,
    load_remediation_spec,
    project_remediation,
)

__all__ = [
    "BASE_REMEDIATION_FILE",
    "PLACEHOLDER_PATTERN",
    "REMEDIATION_DIR",
    "DeviceRemediation",
    "RemediationSpec",
    "command_placeholders",
    "evaluate_conditions",
    "load_remediation_spec",
    "project_remediation",
]
