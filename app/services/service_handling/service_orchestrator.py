"""Discovers and submits service definitions; drives the service-build pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.domain.file_locations import SERVICES_DEF_LOC
from app.logging.logger import get_logger
from app.models import ServiceDescriptor
from app.services.service_handling.service_builder import ServiceBuilder
from app.utils import require
from app.validation.service_definition import ServiceDefinitionDocument

logger = get_logger("app.service_orchestrator")


DefKey = tuple[str, str, str]  # (service, tenant, variant)


SERVICE_DESCRIPTORS: list[ServiceDescriptor] = [
    ServiceDescriptor(
        name="isis",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "isis": "app.services.service_handling.feature_handlers.isis.ISISCoreFeatureHandler"
        },
    ),
    ServiceDescriptor(
        name="sr",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "sr": "app.services.service_handling.feature_handlers.sr.SRFeatureHandler"
        },
    ),
    ServiceDescriptor(
        name="bgp",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "bgp": "app.services.service_handling.feature_handlers.bgp.BGPFeatureHandler",
        },
    ),
    ServiceDescriptor(
        name="evpn_esi",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "evpn_esi": "app.services.service_handling.feature_handlers.evpn_esi.EVPN_ESIFeatureHandler",
        },
    ),
    ServiceDescriptor(
        name="evpn_vpls",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "evpn_vpls": "app.services.service_handling.feature_handlers.evpn_vpls.EVPN_VPLSFeatureHandler",
        },
    ),
    ServiceDescriptor(
        name="vprn",
        definition_schema=ServiceDefinitionDocument,
        feature_handlers={
            "vprn": "app.services.service_handling.feature_handlers.vprn.VPRNFeatureHandler",
        },
    ),
]


class ServiceOrchestrator:
    """
    Deterministically executes ServiceBuilder in the correct order.
    Validates all service YAML definition files.

    Model:

    - Single service file:
          Exactly one `*_def.yaml` file per (service, tenant, variant).
          Each file contains the full service specification.

    Determinism guarantees:

      - Services are executed in SERVICE_DESCRIPTORS order.
      - Tenants are processed in sorted order.
      - Variants are processed in sorted order.

    Constraints:

      - At least one definition file per discovered service/tenant pair.
      - Variant values must be unique within a (service, tenant).
    """

    def __init__(self, *, service_builder: ServiceBuilder):
        self.sb = service_builder
        # Definition YAML parsed during this orchestrator's lifetime. Scoped to
        # the instance on purpose — see _load_service_yaml.
        self._yaml_cache: dict[Path, dict[str, Any]] = {}

    # ----------------------------
    # Public API
    # ----------------------------
    def submit(self) -> None:
        """
        Performs validation/discovery of YAML definition files.
        Calls ServiceBuilder in deterministic order.
        """

        self._validate_yaml_service_files()

        defs_by_key = self._discover_definitions_by_key()
        logger.info(f"Service orchestrator — {len(defs_by_key)} definition(s) found")

        variants_by_service_tenant: dict[tuple[str, str], list[str]] = {}
        for service, tenant, variant in defs_by_key:
            variants_by_service_tenant.setdefault((service, tenant), []).append(variant)

        for key in variants_by_service_tenant:
            variants_by_service_tenant[key].sort()

        for descriptor in SERVICE_DESCRIPTORS:
            service = descriptor.name

            tenants = sorted({tenant for svc, tenant, _variant in defs_by_key if svc == service})
            if not tenants:
                continue

            for tenant in tenants:
                variants = require(
                    variants_by_service_tenant.get((service, tenant)),
                    f"No variants found for service={service!r}, tenant={tenant!r}",
                )

                for variant in variants:
                    def_path = defs_by_key[(service, tenant, variant)]
                    service_data = self._load_service_yaml(def_path)

                    svc_ctx = {
                        "service_data": service_data,
                    }

                    if descriptor.feature_handlers:
                        logger.info(
                            f"  Computing service={service!r} tenant={tenant!r} variant={variant!r}"
                        )
                        self.sb.compute(svc_ctx, descriptor)

        # Remove any ServiceInstance rows whose (service, tenant, variant) no
        # longer corresponds to a definition file — otherwise a renamed or
        # deleted variant leaves a stale row whose frozen `computed` blob keeps
        # leaking into every device render. See ServiceBuilder.prune_orphans.
        pruned = self.sb.prune_orphans(valid_keys=set(defs_by_key))
        if pruned:
            logger.info(
                f"Service orchestrator — pruned {len(pruned)} orphaned instance(s): {pruned}"
            )

    def _validate_yaml_service_files(self) -> None:
        """
        Validate all service definition YAML files.

        Rules:
        - Each definition is validated against the definition schema
          for its service type.
        - Raises on first validation error.
        """
        desc_by_service = {d.name: d for d in SERVICE_DESCRIPTORS}
        defs_by_key = self._discover_definitions_by_key()

        for (service, tenant, variant), def_path in defs_by_key.items():
            descriptor = require(
                desc_by_service.get(service),
                f"No ServiceDescriptor registered for service={service!r} "
                f"(tenant={tenant!r}, variant={variant!r})",
            )
            raw_def = self._load_service_yaml(def_path)
            try:
                descriptor.definition_schema.model_validate(raw_def)
            except Exception as exc:
                raise ValueError(f"Validation failed for '{def_path.name}': {exc}") from exc

    # ----------------------------
    # Discovery
    # ----------------------------
    def _discover_definitions_by_key(self) -> dict[DefKey, Path]:
        """
        Discover all definition YAML files (by filename convention *_def.yaml)
        and index them by (service, tenant, variant). Enforces uniqueness.
        Deterministic: processes files in sorted filename order.
        """
        defs: dict[DefKey, Path] = {}

        for path in sorted(SERVICES_DEF_LOC.path.glob("*_def.yaml"), key=lambda p: p.name):
            data = self._load_service_yaml(path)

            service = require(data.get("service"), f"Missing 'service' in definition: {path}")
            tenant = require(data.get("tenant"), f"Missing 'tenant' in definition: {path}")
            variant = require(data.get("variant"), f"Missing 'variant' in definition: {path}")

            key: DefKey = (service, tenant, variant)
            if key in defs:
                raise ValueError(
                    f"Duplicate service definition for (service, tenant, variant)={key}: "
                    f"{defs[key]} and {path}"
                )

            defs[key] = path

        return defs

    def _load_service_yaml(self, loc: Path) -> dict[str, Any]:
        """Load a definition file, memoised for this orchestrator's lifetime.

        Each definition is parsed more than once per run — ``submit`` discovers
        them, and ``_validate_yaml_service_files`` discovers them again — so
        memoising is worth having. What matters is *how long* it lasts.

        This used to be ``@staticmethod @cache``, a process-wide memo keyed on
        the path. Under ``main.py`` that is harmless: the process ends with the
        run. Under gunicorn the worker lives for days, so the first parse of a
        file was the only one that ever happened. Editing a definition through
        the dashboard's own editor and then running the pipeline from the same
        dashboard silently used the stale copy — no error, and a plausible
        result, which is the worst way for it to fail.

        A ``JobExecutor`` — and so a ``ServiceOrchestrator`` — is built fresh at
        every call site, so binding the cache to the instance makes it correct
        by construction: memoised within a run, gone between runs, with no
        cache-clearing call anyone has to remember.
        """
        loc = loc.resolve()

        cached = self._yaml_cache.get(loc)
        if cached is not None:
            return cached

        # Explicit check rather than `require(loc.exists(), ...)` — require()
        # allows falsy scalars (incl. False) through, so calling it with a
        # bool result is a no-op when the file is missing.
        if not loc.exists():
            raise ValueError(f"YAML file not found: {loc}")

        with loc.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        data = data or {}
        self._yaml_cache[loc] = data
        return data
