"""Resolves IP addressing pool names from YAML policy files using the selector engine."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.domain.file_locations import ADDRESSING_DEF_LOC
from app.models.dataclass_models import DeviceSelectorView
from app.services.selectors.selector_engine import SelectorEngine
from app.services.topology_building.device_factory import Device
from app.validation.addressing import AddressingPolicyDocument


class AddressingPolicyResolver:
    """
    Resolves loopback and P2P prefix pool names for devices from YAML addressing policy files.

    Policies are loaded once via ``install()``. Each policy file defines device selectors
    and maps role names to prefix pool names. Exactly one policy must match any given device;
    ambiguous or missing matches are hard errors.
    """

    def __init__(self, *, selector_engine: SelectorEngine):
        self.selector_engine = selector_engine
        self._policies: list[dict[str, Any]] = []

    def install(self) -> None:
        """
        Load and validate all addressing policy YAML files.
        Build in-memory lookup structures.
        Must be called before any jobs are executed.
        """

        self._policies.clear()

        for path in ADDRESSING_DEF_LOC.path.glob("*.yaml"):
            raw = self._load_yaml(path)
            try:
                AddressingPolicyDocument.model_validate(raw)
            except Exception as exc:
                raise ValueError(f"Validation failed for '{path.name}': {exc}") from exc
            self._policies.append(raw)

        if not self._policies:
            raise ValueError("No addressing policies found")

    def resolve_loopback0_pool(self, selector_view: DeviceSelectorView) -> str:
        """Return the loopback0 prefix pool name for a device."""
        return self._resolve_loopback_pool(selector_view, loopback_name="loopback0")

    def resolve_loopback1_pool(self, selector_view: DeviceSelectorView) -> str:
        """Return the loopback1 prefix pool name for a device."""
        return self._resolve_loopback_pool(selector_view, loopback_name="loopback1")

    def _resolve_loopback_pool(
        self,
        selector_view: DeviceSelectorView,
        *,
        loopback_name: str,
    ) -> str:
        """
        Resolve loopback pool name for a single device selector view.

        Rules:
        - Exactly one addressing policy must match the device
        - Pool is selected based on device role
        - No implicit fallback is allowed
        """

        policy = self._resolve_device_policy(selector_view)
        role_name = selector_view.role.name

        try:
            loopback_cfg = policy["features"]["addressing"][loopback_name]
            by_roles = loopback_cfg["by_roles"]
            return by_roles[role_name]
        except KeyError as err:
            raise ValueError(
                f"No {loopback_name} pool defined for role '{role_name}' "
                f"(device {selector_view.hostname})"
            ) from err

    def _resolve_device_policy(
        self,
        selector_view: DeviceSelectorView,
    ) -> dict[str, Any]:
        """Return the single addressing policy that matches the device; raise if zero or many match."""
        matching: list[dict[str, Any]] = []

        for policy in self._policies:
            sel = policy["selectors"]["devices"]["addressing"]

            if self.selector_engine.select([selector_view], sel):
                matching.append(policy)

        if not matching:
            raise ValueError(f"No addressing policy matches device {selector_view.hostname}")

        if len(matching) > 1:
            raise ValueError(f"Multiple addressing policies match device {selector_view.hostname}")

        return matching[0]

    def resolve_p2p_pool(self, dev_a: Device, dev_b: Device) -> str:
        """
        Resolve P2P prefix pool name for a link between two devices.

        Rules:
        - Addressing policy selector must match BOTH devices
        - Role pairs are unordered (Core-PE == PE-Core)
        - Exactly one policy must match
        - No implicit fallback is allowed
        """

        role_a = dev_a.role.name
        role_b = dev_b.role.name

        matching_policies: list[dict[str, Any]] = []

        for policy in self._policies:
            selectors = policy.get("selectors", {})
            addressing = policy.get("features", {}).get("addressing", {})

            p2p_cfg = addressing.get("p2p")
            if not p2p_cfg:
                continue

            sel = selectors.get("devices", {}).get("addressing")
            if not sel:
                continue

            if not self.selector_engine.select([dev_a], sel):
                continue
            if not self.selector_engine.select([dev_b], sel):
                continue

            matching_policies.append(policy)

        if not matching_policies:
            raise RuntimeError(
                f"No addressing policy matched devices {dev_a.hostname} <-> {dev_b.hostname}"
            )

        if len(matching_policies) > 1:
            raise RuntimeError(
                f"Multiple addressing policies matched devices "
                f"{dev_a.hostname} <-> {dev_b.hostname}"
            )

        policy = matching_policies[0]

        by_roles = policy["features"]["addressing"]["p2p"].get("by_roles", {})

        pair_sorted = "-".join(sorted([role_a, role_b]))
        pair_direct = f"{role_a}-{role_b}"
        pair_reverse = f"{role_b}-{role_a}"

        for key in (pair_sorted, pair_direct, pair_reverse):
            if key in by_roles:
                return by_roles[key]

        raise RuntimeError(
            f"Addressing policy matched devices "
            f"({dev_a.hostname}, {dev_b.hostname}) "
            f"but no p2p pool defined for role pair "
            f"('{pair_sorted}', '{pair_direct}', '{pair_reverse}')"
        )

    def _discover_addressing_definitions(self) -> list[Path]:
        """Return paths of all ``*_def.yaml`` files in the addressing definitions directory."""
        return [
            path
            for path in ADDRESSING_DEF_LOC.path.glob("*.yaml")
            if path.name.endswith("_def.yaml")
        ]

    def _load_addressing_definition(self, yaml_def_file: Path):
        "Gets addressing definition data from YAML definition file"
        return self._load_yaml(yaml_def_file)

    @staticmethod
    def _load_yaml(loc: Path) -> dict[str, Any]:
        """Loads YAML file."""

        loc = loc.resolve()
        # Explicit check — require() allows falsy scalars (incl. bool False)
        # through, so it cannot be used as a guard on a bool expression.
        if not loc.exists():
            raise ValueError(f"YAML file not found: {loc}")

        with loc.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f)
