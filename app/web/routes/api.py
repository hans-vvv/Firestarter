"""JSON API blueprint — machine-readable, read-only device data.

Unlike the dashboard blueprints, which render HTML for a human via Jinja
templates, these routes return JSON for another program to consume. The JSON
shape is therefore a *contract*: callers hardcode the field names, so they are
kept stable and the path is versioned (``/api/v1/...``) — a future breaking
change ships under ``/api/v2/`` rather than silently breaking existing clients.

No authentication: the deployment is reached only from the internal management
network, and these endpoints expose nothing beyond what the dashboard already
shows. That is also why a JSON API is the right shape here — the session-cookie
login gate used by the dashboard would answer an unauthenticated program with a
302 redirect to an HTML login page, which a script cannot follow.
"""

from __future__ import annotations

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from app.web.utils import ce_mgmt_report_rows, devices_with_mgmt_address, list_devices

bp = Blueprint("api", __name__)


@bp.get("/api/v1/devices")
def devices() -> ResponseReturnValue:
    """Return every network device with its role, mgmt IP, and status.

    CEs are left out: they are logical devices managed through their PE and
    carry no management address of their own — their record lives under
    ``/api/v1/ce-mgmt`` instead. "Is a CE" is decided structurally (the device
    has a CE-management record), never by a role-name list, so a new CE role
    needs no change here. ``mgmt_ip`` is ``null`` for a device that carries no
    management address.
    """
    mgmt_by_host = dict(devices_with_mgmt_address())  # {hostname: mgmt_ip}
    ce_hostnames = {r["ce_hostname"] for r in ce_mgmt_report_rows()}
    payload = [
        {
            "hostname": d["hostname"],
            "role": d["role"],
            "mgmt_ip": mgmt_by_host.get(d["hostname"]),
            "status": d["status"],
        }
        for d in list_devices()
        if d["hostname"] not in ce_hostnames
    ]
    return jsonify(payload)


def _ce_mgmt(row: dict) -> dict:
    """Project a report row to the six-key contract (GW = the report's VRRP gateway)."""
    return {
        "pe1": row["pe1"],
        "port1": row["port1"],
        "pe2": row["pe2"],
        "port2": row["port2"],
        "mgmt_ip": row["mgmt_ip"],
        "GW": row["vrrp_gateway"],
    }


@bp.get("/api/v1/ce-mgmt")
def ce_mgmt_all() -> ResponseReturnValue:
    """Return the management record for every CE, as a list.

    Each item carries the six-key record plus a ``hostname`` so callers can tell
    the rows apart while iterating.
    """
    payload = [{"hostname": r["ce_hostname"], **_ce_mgmt(r)} for r in ce_mgmt_report_rows()]
    return jsonify(payload)


@bp.get("/api/v1/ce-mgmt/<hostname>")
def ce_mgmt_one(hostname: str) -> ResponseReturnValue:
    """Return one CE's management record, or 404 if there is none for *hostname*."""
    for row in ce_mgmt_report_rows():
        if row["ce_hostname"] == hostname:
            return jsonify(_ce_mgmt(row))
    return jsonify({"error": f"No CE management record for '{hostname}'"}), 404
