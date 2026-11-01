"""
Validation rules applied to the Excel input workbook before topology job creation.

This module performs pre-database consistency and integrity checks across the
Excel sheets used to import devices, PE devices, cables, and half-open
rings. It returns structured validation errors rather than raising immediately,
allowing callers to collect and present all input issues in one pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ValidationError:
    sheet: str
    row: int | None
    column: str | None
    message: str


def _excel_row(idx: Any) -> int:
    # pandas idx is 0-based; Excel rows start at 1 and include header row
    return int(idx) + 2


def _norm(v: object) -> str:
    """
    Normalize a cell value to a stripped string.
    """
    return "" if v is None else str(v).strip()


def _parse_device_cell(cell: object) -> list[str]:
    """
    Parse a device cell that may contain one or two comma-separated hostnames.

    Used for fields such as `DistDevices.DeviceName` and HalfOpenRings site
    cells, where the allowed formats are:
    - ``"dev"``
    - ``"devA,devB"``

    Parameters
    ----------
    cell : object
        Raw cell value.

    Returns
    -------
    list[str]
        Parsed hostnames. The result is empty for blank input and otherwise
        contains one or two non-empty names.
    """
    txt = _norm(cell)
    if not txt:
        return []
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    return parts


# ============================================================
# Devices
# ============================================================
def validate_devices(df: pd.DataFrame) -> tuple[list[ValidationError], set[str]]:
    """
    Validate the Devices sheet.

    This check verifies that:
    - `DeviceName` exists and is non-empty
    - `Tenant` is present for each device
    - device names are unique within the sheet

    Parameters
    ----------
    df : pandas.DataFrame
        Devices sheet data.

    Returns
    -------
    tuple[list[ValidationError], set[str]]
        A tuple containing:
        - list of validation errors
        - set of valid device names encountered in the sheet
    """
    errors: list[ValidationError] = []
    devices: list[str] = []

    # required columns (assumes metadata checks already ran, but keep defensive)
    if "DeviceName" not in df.columns:
        return ([ValidationError("Devices", None, "DeviceName", "Missing column")], set())

    for idx, row in df.iterrows():
        name = _norm(row.get("DeviceName"))
        tenant = _norm(row.get("Tenant"))
        if not name:
            errors.append(
                ValidationError("Devices", _excel_row(idx), "DeviceName", "Empty DeviceName")
            )
            continue
        if not tenant:
            errors.append(
                ValidationError("Devices", _excel_row(idx), "Tenant", f"Empty Tenant for {name}")
            )

        devices.append(name)

    # uniqueness
    s = pd.Series(devices)
    for dup_name in sorted(set(s[s.duplicated()].tolist())):
        # point to first duplicate row (good enough)
        first_dup_idx = df.index[df["DeviceName"].astype(str).str.strip() == dup_name].tolist()
        rownum = _excel_row(first_dup_idx[0]) if first_dup_idx else None
        errors.append(
            ValidationError("Devices", rownum, "DeviceName", f"Duplicate device '{dup_name}'")
        )

    return errors, set(devices)


# ============================================================
# DistDevices
# ============================================================
def validate_dist_devices(
    df: pd.DataFrame,
) -> tuple[list[ValidationError], set[str], set[tuple[str, str]]]:
    """
    Validate the DistDevices sheet.

    This check verifies required columns, parses `DeviceName` cells containing
    one or two devices, enforces per-row tenant presence, records PE pairs for
    two-device entries, and detects duplicate devices across the sheet.

    Parameters
    ----------
    df : pandas.DataFrame
        DistDevices sheet data.

    Returns
    -------
    tuple[list[ValidationError], set[str], set[tuple[str, str]]]
        A tuple containing:
        - list of validation errors
        - set of device names referenced in the sheet
        - set of normalized device pairs declared in the sheet
    """
    errors: list[ValidationError] = []
    seen: list[str] = []
    pe_pairs: set[tuple[str, str]] = set()

    required = {"DeviceName", "RoleName", "SiteName", "ModelName", "Tenant"}
    missing = [c for c in required if c not in df.columns]
    for c in missing:
        errors.append(ValidationError("DistDevices", None, c, "Missing column"))
    if missing:
        return errors, set(), set()

    for idx, row in df.iterrows():
        cell = row.get("DeviceName")
        names = _parse_device_cell(cell)

        if not names:
            errors.append(
                ValidationError("DistDevices", _excel_row(idx), "DeviceName", "Empty DeviceName")
            )
            continue

        if len(names) == 2:
            a, b = sorted(names)
            pe_pairs.add((a, b))
        elif len(names) == 1:
            pass
        else:
            errors.append(
                ValidationError(
                    "DistDevices",
                    _excel_row(idx),
                    "DeviceName",
                    f"Invalid DeviceName cell '{_norm(cell)}' (expected 'a' or 'a,b')",
                )
            )
            continue

        tenant = _norm(row.get("Tenant"))
        if not tenant:
            errors.append(
                ValidationError(
                    "DistDevices", _excel_row(idx), "Tenant", f"Empty Tenant for '{_norm(cell)}'"
                )
            )

        for n in names:
            seen.append(n)

    s = pd.Series(seen)
    for dup_name in sorted(set(s[s.duplicated()].tolist())):
        errors.append(
            ValidationError(
                "DistDevices",
                None,
                "DeviceName",
                f"Duplicate device '{dup_name}' in DistDevices",
            )
        )

    return errors, set(seen), pe_pairs


# ============================================================
# Mutual exclusivity: Devices vs DistDevices
# ============================================================
def validate_mutual_exclusive(
    devices: set[str],
    dist_devices: set[str],
) -> list[ValidationError]:
    """
    Ensure Devices and DistDevices are mutually exclusive.

    Parameters
    ----------
    devices : set[str]
        Device names declared in the Devices sheet.
    dist_devices : set[str]
        Device names declared in the DistDevices sheet.

    Returns
    -------
    list[ValidationError]
        Validation errors for any device appearing in both sets.
    """
    errors: list[ValidationError] = []
    overlap = sorted(devices & dist_devices)
    for name in overlap:
        errors.append(
            ValidationError(
                "Devices/DistDevices",
                None,
                "DeviceName",
                f"Device '{name}' appears in both Devices and DistDevices (must be mutually exclusive)",
            )
        )
    return errors


# ============================================================
# Cables
# ============================================================
def validate_cables(
    df: pd.DataFrame,
    *,
    all_devices: set[str],
) -> list[ValidationError]:
    """
    Validate the Cables sheet.

    Rules
    -----
    - Device_a and Device_b are required.
    - Iface_a and Iface_b are optional because they may be auto-assigned later.
    - Referenced devices must exist in ``all_devices``.
    - A cable may not connect a device/interface to the exact same device/interface.
    - Duplicate cables are detected independent of endpoint ordering, but only
      when both endpoints are fully specified (device + interface on both sides).

    Parameters
    ----------
    df : pandas.DataFrame
        Cables sheet data.
    all_devices : set[str]
        Set of all known devices from Devices and DistDevices.

    Returns
    -------
    list[ValidationError]
        Validation errors found in the sheet.
    """
    errors: list[ValidationError] = []

    required_columns = {"Device_a", "Iface_a", "Device_b", "Iface_b"}
    missing_columns = [col for col in required_columns if col not in df.columns]
    for col in missing_columns:
        errors.append(ValidationError("Cables", None, col, "Missing column"))
    if missing_columns:
        return errors

    seen_links: set[tuple[str, str, str, str]] = set()

    for idx, row in df.iterrows():
        row_num = _excel_row(idx)

        device_a = _norm(row.get("Device_a"))
        iface_a = _norm(row.get("Iface_a"))
        device_b = _norm(row.get("Device_b"))
        iface_b = _norm(row.get("Iface_b"))

        # Required fields: devices only
        if not device_a:
            errors.append(ValidationError("Cables", row_num, "Device_a", "Empty Device_a"))
        if not device_b:
            errors.append(ValidationError("Cables", row_num, "Device_b", "Empty Device_b"))

        # Cannot continue meaningfully without both devices
        if not (device_a and device_b):
            continue

        # Known device checks
        if device_a not in all_devices:
            errors.append(
                ValidationError("Cables", row_num, "Device_a", f"Unknown device '{device_a}'")
            )
        if device_b not in all_devices:
            errors.append(
                ValidationError("Cables", row_num, "Device_b", f"Unknown device '{device_b}'")
            )

        # Invalid exact self-link only when both interfaces are present
        if device_a == device_b and iface_a and iface_b and iface_a == iface_b:
            errors.append(
                ValidationError(
                    "Cables",
                    row_num,
                    None,
                    "Cable endpoint A equals endpoint B",
                )
            )

        # Duplicate detection only when both endpoints are fully specified
        if iface_a and iface_b:
            end1 = (device_a, iface_a)
            end2 = (device_b, iface_b)

            if end2 < end1:
                end1, end2 = end2, end1

            key = (end1[0], end1[1], end2[0], end2[1])

            if key in seen_links:
                errors.append(
                    ValidationError(
                        "Cables",
                        row_num,
                        None,
                        f"Duplicate cable '{key}'",
                    )
                )
            else:
                seen_links.add(key)

    return errors


# ============================================================
# HalfOpenRings
# ============================================================
def validate_half_open_rings(
    df: pd.DataFrame,
    *,
    dist_devices: set[str],
    pe_pairs: set[tuple[str, str]],
) -> list[ValidationError]:
    """
    Validate the HalfOpenRings sheet.

    This check verifies that ring terminations are present, device cells contain
    one or two valid device names, referenced devices exist in DistDevices,
    devices do not appear in multiple rings, and PE pairs used in rings are
    consistent with pair declarations from DistDevices.

    Parameters
    ----------
    df : pandas.DataFrame
        HalfOpenRings sheet data.
    dist_devices : set[str]
        Known PE devices.
    pe_pairs : set[tuple[str, str]]
        Normalized PE pairs declared in DistDevices.

    Returns
    -------
    list[ValidationError]
        Validation errors found in the sheet.
    """
    errors: list[ValidationError] = []

    if "Termination_site_a" not in df.columns:
        errors.append(
            ValidationError("HalfOpenRings", None, "Termination_site_a", "Missing column")
        )
    if "Termination_site_b" not in df.columns:
        errors.append(
            ValidationError("HalfOpenRings", None, "Termination_site_b", "Missing column")
        )
    if errors:
        return errors

    device_to_ring: dict[str, str] = {}
    ring_devices: set[str] = set()
    ring_pairs: set[tuple[str, str]] = set()

    for idx, row in df.iterrows():
        site_a = _norm(row.get("Termination_site_a"))
        site_b = _norm(row.get("Termination_site_b"))

        if not site_a:
            errors.append(
                ValidationError(
                    "HalfOpenRings",
                    _excel_row(idx),
                    "Termination_site_a",
                    "Empty Termination_site_a",
                )
            )
        if not site_b:
            errors.append(
                ValidationError(
                    "HalfOpenRings",
                    _excel_row(idx),
                    "Termination_site_b",
                    "Empty Termination_site_b",
                )
            )
        if not (site_a and site_b):
            continue

        ring_id = ":".join(sorted([site_a, site_b]))

        for col, val in row.items():
            if col in ("Termination_site_a", "Termination_site_b"):
                continue
            if pd.isna(val):
                continue

            cell = _norm(val)
            if not cell:
                continue  # allowed: empty site columns

            devs = _parse_device_cell(cell)

            # cell validation: only 1 or 2 entries
            if len(devs) not in (1, 2):
                errors.append(
                    ValidationError(
                        "HalfOpenRings",
                        _excel_row(idx),
                        str(col),
                        f"Invalid cell '{cell}' (expected 'a' or 'a,b')",
                    )
                )
                continue

            if len(devs) == 2:
                a, b = sorted(devs)
                pair = (a, b)
                ring_pairs.add(pair)

                if pair not in pe_pairs:
                    errors.append(
                        ValidationError(
                            "HalfOpenRings",
                            _excel_row(idx),
                            str(col),
                            f"Devices '{devs[0]},{devs[1]}' are paired in HalfOpenRings but not paired in DistDevices",
                        )
                    )

            for dev in devs:
                ring_devices.add(dev)

                if dev not in dist_devices:
                    errors.append(
                        ValidationError(
                            "HalfOpenRings",
                            _excel_row(idx),
                            str(col),
                            f"Device '{dev}' referenced in ring but not present in DistDevices",
                        )
                    )

                prev = device_to_ring.get(dev)
                if prev and prev != ring_id:
                    errors.append(
                        ValidationError(
                            "HalfOpenRings",
                            _excel_row(idx),
                            str(col),
                            f"Device '{dev}' appears in two rings ({prev} and {ring_id})",
                        )
                    )
                else:
                    device_to_ring[dev] = ring_id

    # reverse check:
    # if a PE pair is used in HalfOpenRings, it must appear as a pair there
    for a, b in pe_pairs:
        if (a in ring_devices or b in ring_devices) and (a, b) not in ring_pairs:
            errors.append(
                ValidationError(
                    "HalfOpenRings",
                    None,
                    None,
                    f"Devices '{a},{b}' are declared as pair in DistDevices but are not paired in HalfOpenRings",
                )
            )

    return errors


def collect_used_sites(
    df_devices: pd.DataFrame,
    df_dist: pd.DataFrame | None,
) -> set[str]:
    """
    Collect all site names referenced across Devices and DistDevices.

    Returns
    -------
    set[str]
        Normalized set of site names.
    """
    sites: set[str] = set()

    if "Site" in df_devices.columns:
        sites |= {str(s).strip() for s in df_devices["Site"] if pd.notna(s) and str(s).strip()}

    if df_dist is not None and "SiteName" in df_dist.columns:
        sites |= {str(s).strip() for s in df_dist["SiteName"] if pd.notna(s) and str(s).strip()}

    return sites


def validate_site_sheet(df_sites: pd.DataFrame) -> list[ValidationError]:
    """
    Validate the Site sheet for duplicate site definitions.

    Each ``SiteName`` must be declared exactly once in the Site sheet: it is the
    authoritative list of sites seeded into the database, so a repeated name
    would silently collapse into a single row during seeding while suggesting to
    the reader that two distinct sites exist. Duplicates are reported here,
    pointing at the first offending row.

    Blank rows are ignored — the cross-reference check in ``validate_sites`` and
    the seeding logic both drop empty site names, so an empty cell is not a
    duplicate.

    Parameters
    ----------
    df_sites : pandas.DataFrame
        Site sheet data.

    Returns
    -------
    list[ValidationError]
        One error per duplicated site name, ordered by name.
    """
    if "SiteName" not in df_sites.columns:
        return [ValidationError("Site", None, "SiteName", "Missing column")]

    # Non-blank site names only. A pandas NaN survives _norm as the literal
    # "nan" string, so trailing empty rows are dropped up front to avoid
    # false-flagging them as duplicates of each other.
    names = [str(v).strip() for v in df_sites["SiteName"] if pd.notna(v) and str(v).strip()]

    errors: list[ValidationError] = []
    s = pd.Series(names)
    for dup_name in sorted(set(s[s.duplicated()].tolist())):
        normalized = df_sites["SiteName"].apply(lambda v: str(v).strip() if pd.notna(v) else "")
        first_dup_idx = df_sites.index[normalized == dup_name].tolist()
        rownum = _excel_row(first_dup_idx[0]) if first_dup_idx else None
        errors.append(
            ValidationError("Site", rownum, "SiteName", f"Duplicate SiteName '{dup_name}'")
        )

    return errors


def validate_sites(
    df_sites: pd.DataFrame,
    *,
    used_sites: set[str],
) -> list[ValidationError]:
    """
    Ensure all referenced sites exist in Sites sheet.
    """
    errors: list[ValidationError] = []

    if "SiteName" not in df_sites.columns:
        return [ValidationError("Sites", None, "SiteName", "Missing column")]

    known_sites = {str(s).strip() for s in df_sites["SiteName"] if pd.notna(s) and str(s).strip()}

    missing = sorted(used_sites - known_sites)

    for site in missing:
        errors.append(
            ValidationError(
                "Sites",
                None,
                "SiteName",
                f"Site '{site}' is referenced but not defined in Sites sheet",
            )
        )

    return errors


# ============================================================
# Orchestrator (Excel-only, before DB)
# ============================================================
def validate_excel_inputs(
    *,
    df_devices: pd.DataFrame,
    df_dist: pd.DataFrame | None,
    df_cables: pd.DataFrame,
    df_half_open_rings: pd.DataFrame | None,
    df_sites: pd.DataFrame,
) -> list[ValidationError]:
    """
    Run all Excel-level validation checks before database operations.

    The validation flow includes:
    - Devices sheet validation
    - optional DistDevices sheet validation
    - mutual exclusivity checks between Devices and DistDevices
    - Cables sheet validation against the combined device set
    - optional HalfOpenRings sheet validation
    - Site sheet validation (unique site definitions + referenced-site presence)

    Parameters
    ----------
    df_devices : pandas.DataFrame
        Devices sheet data.
    df_dist : pandas.DataFrame | None
        DistDevices sheet data, if present.
    df_cables : pandas.DataFrame
        Cables sheet data.
    df_half_open_rings : pandas.DataFrame | None
        HalfOpenRings sheet data, if present.

    Returns
    -------
    list[ValidationError]
        All validation errors collected across the provided sheets.
    """
    errors: list[ValidationError] = []
    dist_devices: set[str] = set()
    pe_pairs: set[tuple[str, str]] = set()

    dev_err, devices = validate_devices(df_devices)
    errors += dev_err

    if df_dist is not None:
        dist_err, dist_devices, pe_pairs = validate_dist_devices(df_dist)
        errors += dist_err

    errors += validate_mutual_exclusive(devices, dist_devices)

    all_devices = devices | dist_devices
    errors += validate_cables(df_cables, all_devices=all_devices)

    if df_half_open_rings is not None:
        errors += validate_half_open_rings(
            df_half_open_rings, dist_devices=dist_devices, pe_pairs=pe_pairs
        )

    errors += validate_site_sheet(df_sites)

    used_sites = collect_used_sites(df_devices, df_dist)
    errors += validate_sites(df_sites, used_sites=used_sites)

    return errors
