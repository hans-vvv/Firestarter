"""Renders device configurations via Jinja2 templates and manages the artifacts directory."""

from __future__ import annotations

import copy
import shutil
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.file_locations import ARTIFACTS_LOC
from app.models import ServiceInstance
from app.repositories import get_device_by_hostname
from app.services.context.device_context import DeviceContextComposer
from app.services.context.services_context import compose_services
from app.utils import (
    breakout_name,
    coherent_frequency,
    deep_merge,
    peer_ip_on_p2p,
    require,
)

TEMPLATE_MAP = {
    "7250-IXR-e2-400": Path("app/services/templates/sros"),
    "7250-IXR-e2-100": Path("app/services/templates/sros"),
    "7250-IXR-X3": Path("app/services/templates/sros"),
    "7750-SR-1x-48d": Path("app/services/templates/sros"),
    "7750-SR-1x-46s": Path("app/services/templates/sros"),
}

ARTIFACTS_DIR = ARTIFACTS_LOC.path
LATEST_DIR = ARTIFACTS_DIR / "latest"


class Printer:
    """
    Renders device configuration using Jinja2 templates.

    - device_ctx is created once per device render
    - service composition mutates the same device_ctx. This
      context is assumed to have all interfaces attached.

    Disk layout (managed by write_to_disk)
    ---------------------------------------
    app/artifacts/
        latest/                 ← always the most recent render
            <hostname>.cfg
            datetime.txt        ← UTC timestamp of this render
        2024-11-03T14:22:05/    ← previous render, archived by its timestamp
            <hostname>.cfg
            datetime.txt
    """

    def __init__(
        self,
        *,
        session: Session,
        exclude_instance_ids: set[int] | None = None,
        exclude_objects: dict[int, list[tuple[str, str, str]]] | None = None,
    ):
        self.session = session
        # ServiceInstance rows to skip during the service-intent merge. The
        # per-service snippet view renders a device *without* a chosen subset of
        # its services, then diffs against the full render to isolate exactly
        # what those services contribute. ``None`` (the default) merges every
        # instance — the normal full-config render, unchanged.
        self.exclude_instance_ids = exclude_instance_ids or set()
        # Finer-grained exclusion than whole instances: maps a ServiceInstance id
        # to the named objects to drop from its computed blob before merging,
        # each as ``(ctx_key, variant, object_name)`` — e.g. a single VPRN inside
        # a ``vprn`` instance that bundles several. Lets the snippet view
        # isolate one VPRN/VPLS's lines, not just a whole service.
        self.exclude_objects = exclude_objects or {}
        self.dcc = DeviceContextComposer(session=session)
        self._env_cache: dict[Path, Environment] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_device(self, *, hostname: str) -> str:
        """Render and return the full configuration string for a single device."""
        return self._render(hostname=hostname)

    def print_all(self) -> dict[str, str]:
        """Render all devices that have computed service data; return hostname → config mapping."""
        result: dict[str, str] = {}

        for hostname in self._collect_devices():
            result[hostname] = self.render_device(hostname=hostname)

        return result

    def write_to_disk(self) -> dict[str, str]:
        """Render all devices and write configs to ``app/artifacts/latest``.

        If ``latest/`` already contains a previous render it is moved to a
        sibling directory named after the timestamp recorded in its own
        ``datetime.txt``.  This preserves every historical render without
        relying on the wall-clock time of the *current* run for the archive
        name, which would be ambiguous if a render was interrupted mid-way.

        Returning the rendered text directly avoids reading the files back
        from disk in the compliance Runner.
        """
        self._archive_existing_latest()

        LATEST_DIR.mkdir(parents=True, exist_ok=True)

        rendered = self.print_all()
        now = datetime.now(UTC)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%S")

        for hostname, config in rendered.items():
            safe_name = hostname.replace("/", "_").replace(" ", "_")
            cfg_path = LATEST_DIR / f"{safe_name}.cfg"
            text = config if config.endswith("\n") else config + "\n"
            cfg_path.write_text(text, encoding="utf-8")

        (LATEST_DIR / "datetime.txt").write_text(timestamp + "\n", encoding="utf-8")

        return rendered

    # ------------------------------------------------------------------
    # Jinja environment
    # ------------------------------------------------------------------

    def _get_env(self, template_dir: Path) -> Environment:
        env = self._env_cache.get(template_dir)
        if env is not None:
            return env

        env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["peer_ip_on_p2p"] = peer_ip_on_p2p
        env.filters["breakout_name"] = breakout_name
        env.filters["coherent_frequency"] = coherent_frequency

        self._env_cache[template_dir] = env
        return env

    # ------------------------------------------------------------------
    # Services
    # ------------------------------------------------------------------

    @cached_property
    def _collect_services_computed(self) -> dict[str, Any]:
        """
        Merges information from services
        computed by feature handlers
        """
        result: dict[str, Any] = {}

        service_instances = self.session.scalars(
            select(ServiceInstance).where(ServiceInstance.computed.is_not({}))
        )

        for svc_inst in service_instances:
            if svc_inst.id in self.exclude_instance_ids:
                continue
            computed = svc_inst.computed
            prunes = self.exclude_objects.get(svc_inst.id)
            if prunes:
                computed = self._prune_objects(computed, prunes)
            result = deep_merge(result, computed)

        # jprint(result["pe1.tst-001"])

        return result

    @staticmethod
    def _prune_objects(
        computed: dict[str, Any],
        prunes: list[tuple[str, str, str]],
    ) -> dict[str, Any]:
        """Return a deep copy of ``computed`` with the named objects removed.

        ``computed`` is a ``ServiceInstance`` blob keyed by hostname; the
        objects of a VPRN/VPLS service live at ``[host][ctx_key]["variant"]
        [variant][object_name]``. Each prune is ``(ctx_key, variant,
        object_name)`` and is dropped from every host that carries it.

        The copy is deep so the ORM-attached JSON blob is never mutated — the
        snippet view renders this pruned copy only to diff it against the full
        render. Missing paths are silently ignored (an object the user selected
        that simply isn't present on a given host is not an error).
        """
        pruned = copy.deepcopy(computed)
        for host_ctx in pruned.values():
            if not isinstance(host_ctx, dict):
                continue
            for ctx_key, variant, object_name in prunes:
                variants = host_ctx.get(ctx_key, {}).get("variant", {})
                variants.get(variant, {}).pop(object_name, None)
        return pruned

    def _collect_devices(self) -> list[str]:
        """
        Returns all devices with any service present
        """
        return list(self._collect_services_computed.keys())

    def _build_service_intent(
        self,
        *,
        device_ctx: dict[str, Any],
        hostname: str,
    ) -> dict[str, Any]:
        """
        Returns complete global and interface level intent for all
        services per device
        """
        service_intent = self._collect_services_computed.get(hostname, {})

        return compose_services(
            device_ctx=device_ctx,
            service_intent=service_intent,
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _select_base_template(self, *, hostname: str) -> Path:
        """
        Select appropriate J2 template based on device model name
        """
        device = require(
            get_device_by_hostname(self.session, hostname=hostname),
            f"No DB row present for {hostname}",
        )

        return TEMPLATE_MAP[device.model_name]

    def _build_render_ctx(self, *, hostname: str) -> dict[str, Any]:
        """
        Final render context for a device.
        """
        device_ctx = self.dcc.compose(hostname=hostname)

        service_ctx = self._build_service_intent(
            device_ctx=device_ctx,
            hostname=hostname,
        )

        result = {
            **device_ctx,
            **service_ctx,
        }
        # jprint(result)
        return result

    def _render(self, *, hostname: str) -> str:
        """
        Renders final device config
        """
        template_dir = self._select_base_template(hostname=hostname)
        env = self._get_env(template_dir)

        template = env.get_template("base.j2")
        ctx = self._build_render_ctx(hostname=hostname)

        return template.render(**ctx)

    # ------------------------------------------------------------------
    # Disk / archiving helpers
    # ------------------------------------------------------------------

    def _archive_existing_latest(self) -> None:
        """Move ``latest/`` to a timestamped sibling directory.

        The archive directory name is taken from the ``datetime.txt`` already
        inside ``latest/``.  If that file is missing (e.g. a partial previous
        run) we fall back to the directory's mtime so nothing is ever silently
        discarded.
        """
        if not LATEST_DIR.exists():
            return

        datetime_file = LATEST_DIR / "datetime.txt"
        if datetime_file.exists():
            timestamp = datetime_file.read_text(encoding="utf-8").strip()
        else:
            mtime = LATEST_DIR.stat().st_mtime
            timestamp = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")

        # Sanitise the timestamp so it is a valid directory name on all OSes.
        safe_ts = timestamp.replace(":", "-")
        archive_dir = ARTIFACTS_DIR / safe_ts

        # Guard against a re-run within the same second producing a collision.
        if archive_dir.exists():
            suffix = 1
            while (candidate := ARTIFACTS_DIR / f"{safe_ts}_{suffix}").exists():
                suffix += 1
            archive_dir = candidate

        shutil.move(str(LATEST_DIR), str(archive_dir))
