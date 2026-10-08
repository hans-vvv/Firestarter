"""Dynamic catalogue of service-definition YAML files.

Scans the service-definitions directory under the data root at call time, reads
only the header fields (service / tenant / variant) from each file, and returns
a structured catalogue used by the services blueprint to build the index
page and resolve editor URLs without any hardcoded filename-to-URL mapping.

The catalogue groups files into two visual categories:

    infra     — network-wide protocols: ISIS, BGP, SR (any variant), EVPN-ESI
    services  — customer-facing VPRN and EVPN-VPLS definitions (any variant)

Category assignment is driven by the *service* field parsed from each file
header; filename patterns are never used.  This means the catalogue adapts
automatically when files are added or removed — a server that holds only lab
files will show only lab entries.

URL slugs are also derived from parsed fields (service / tenant / variant) so
that the same URL can be reconstructed from either end: the index page builds
links from slugs, and the editor route resolves a slug back to the correct
file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from app.domain.file_locations import SERVICES_DEF_LOC

# The service-definition YAML is environment data under the data root, not code.
# Resolved via the registry so the Services page works whatever the data root is
# (a plain checkout's data/, or an external FIRESTARTER_DATA root / container volume).
_DEFS_DIR = SERVICES_DEF_LOC.path

# ── category metadata (display order, label, Bootstrap icon, colour class) ──

CATEGORIES: dict[str, dict] = {
    "infra": {
        "label": "Infrastructure",
        "description": "Network-wide protocols — ISIS, BGP, SR, EVPN-ESI",
        "icon": "bi-diagram-3",
        "colour": "secondary",
    },
    "services": {
        "label": "Services",
        "description": "VPRN and EVPN-VPLS services exposed to the CEs",
        "icon": "bi-hdd-network",
        "colour": "primary",
    },
}

_INFRA_SERVICES = ("isis", "bgp", "evpn_esi", "sr")
_CUSTOMER_SERVICES = {"vprn": "vprn", "evpn_vpls": "vpls"}


def _slugify(value: str) -> str:
    return value.replace("_", "-")


# ── core dataclasses ─────────────────────────────────────────────────────────


@dataclass
class ServiceDef:
    """One entry in the catalogue — represents a single YAML def file."""

    category: str  # infra | services
    label: str  # human-readable card title
    url_slug: str  # path suffix after /services/
    filename: str  # bare filename (no directory)

    # display hint for the badge
    env_badge: str = ""  # the tenant


@dataclass
class Catalogue:
    """All discovered service definitions, pre-grouped by category."""

    infra: list[ServiceDef] = field(default_factory=list)
    services: list[ServiceDef] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([self.infra, self.services])

    def all_entries(self) -> list[ServiceDef]:
        return self.infra + self.services


# ── classification ────────────────────────────────────────────────────────────


def _classify(service: str, tenant: str, variant: str, filename: str) -> ServiceDef | None:
    """Map parsed YAML header fields to a ServiceDef, or None if unrecognised.

    Infrastructure slugs are ``infra/<tenant>/<service>``; SR, which exists in
    one variant per role group, appends its variant (``infra/lab/sr-pe``).
    Customer services are ``services/<tenant>/<vprn|vpls>/<variant>`` so every
    variant of a VPRN or VPLS definition gets its own card.
    """
    if service in _INFRA_SERVICES:
        if service == "sr":
            slug = f"infra/{tenant}/sr-{_slugify(variant)}"
            label = f"SR {variant} · {tenant}"
        else:
            slug = f"infra/{tenant}/{_slugify(service)}"
            label = f"{service.upper().replace('_', '-')} · {tenant}"
        return ServiceDef("infra", label, slug, filename, env_badge=tenant)

    if service in _CUSTOMER_SERVICES:
        svc = _CUSTOMER_SERVICES[service]
        slug = f"services/{tenant}/{svc}/{_slugify(variant)}"
        label = f"{svc.upper()} {variant} · {tenant}"
        return ServiceDef("services", label, slug, filename, env_badge=tenant)

    return None  # unrecognised — excluded from catalogue


# ── public API ────────────────────────────────────────────────────────────────


def _read_header(path: Path) -> tuple[str, str, str] | None:
    """Return the (service, tenant, variant) identity from a YAML file.

    The whole document is parsed (these files are small) but only the three
    identity fields are used; missing/blank fields or non-dict roots yield None.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        if not isinstance(doc, dict):
            return None
        service = doc.get("service", "")
        tenant = doc.get("tenant", "")
        variant = doc.get("variant", "")
        if not (service and tenant and variant):
            return None
        return str(service), str(tenant), str(variant)
    except Exception:
        return None


def build_catalogue(*, defs_dir: Path = _DEFS_DIR) -> Catalogue:
    """Scan *defs_dir* and return a populated :class:`Catalogue`.

    Only files whose names end in ``_def.yaml`` or ``_def.yml`` are considered.
    Files that cannot be parsed or whose fields do not match any known category
    are silently skipped, so the page renders with whatever is present on disk.
    """
    cat = Catalogue()
    slug_seen: set[str] = set()

    patterns = list(defs_dir.glob("*_def.yaml")) + list(defs_dir.glob("*_def.yml"))
    for path in sorted(patterns):
        header = _read_header(path)
        if header is None:
            continue
        service, tenant, variant = header
        entry = _classify(service, tenant, variant, path.name)
        if entry is None:
            continue
        # Deduplicate by slug (protects against duplicate files with same content)
        if entry.url_slug in slug_seen:
            continue
        slug_seen.add(entry.url_slug)

        if entry.category == "infra":
            cat.infra.append(entry)
        else:
            cat.services.append(entry)

    return cat


def resolve_slug(slug: str, *, defs_dir: Path = _DEFS_DIR) -> Path | None:
    """Return the absolute Path for a URL slug, or None if not found.

    Rebuilds the catalogue and does a linear search — acceptable given the
    small number of definition files and the infrequency of editor requests.
    Path traversal is impossible because the slug is matched against catalogue
    entries (derived from parsed file content), never concatenated with the
    filesystem path directly.
    """
    for entry in build_catalogue(defs_dir=defs_dir).all_entries():
        if entry.url_slug == slug:
            return defs_dir / entry.filename
    return None
