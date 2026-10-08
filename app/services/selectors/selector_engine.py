"""Rule-based device selector engine used to match devices against YAML policy selectors."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from sqlalchemy.orm import Session

from app.models.dataclass_models import DeviceSelectorView, RoleView

type SelectorViews = list["DeviceSelectorView"]
T = TypeVar("T")


class SelectorEngine:
    """
    Engine to filter/select devices based on structured selector configuration.

    Supports logical AND, OR, and exclusion rules applied to device attributes
    such as role, labels, and hostname.

    Usage:
        - Use select() method with devices list and a selector config dict.
        - Selector config may contain 'match' (AND rules), 'any' (OR rules),
          and 'exclude' (exclusion rules).

    Selector semantics (IMPORTANT):
    -------------------------------
    - Scalar matching is supported for labels and roles:
        labels:
          fabric: fabric1

    - Explicit include/exclude is supported:
        labels:
          fabric:
            include: ["fabric1"]
            exclude: ["fabric2"]

    - List shorthand is NOT supported:
        labels:
          fabric: ["fabric1"]  -> NOT SUPPORTED

    Rationale:
        Explicit structures avoid ambiguity and simplify validation.
    """

    def __init__(self, session: Session):
        """Initialize with database session."""
        self.session = session

    # ------------------------------
    # Public entry point
    # ------------------------------
    def select(self, devices: Iterable[T], cfg: dict) -> list[T]:
        """
        Select devices matching the given selector configuration.

        This method accepts domain objects (e.g. ORM Device instances)
        or selector-compatible views. Domain objects are adapted internally
        to a selector-compatible shape. Reason: keep Pylance happy while
        duck-typing.

        cfg structure:
        {
            "match": {...},   # AND rules
            "any": [...],     # OR rules
            "exclude": {...}  # final filter
        }

        Evaluation order:
        1. match   (AND)
        2. any     (OR, optional)
        3. exclude (final subtraction)
        """

        # --- normalize inputs to selector views ---
        selector_views: SelectorViews = []
        view_to_original: dict[int, T] = {}

        for obj in devices:
            view = self._to_selector_view(obj)
            selector_views.append(view)
            view_to_original[id(view)] = obj

        match_cfg = cfg.get("match", {})
        any_cfg = cfg.get("any", [])
        exclude_cfg = cfg.get("exclude", {})

        # 1. Apply AND rules
        result_views = self._apply_match_rules(selector_views, match_cfg)

        # 2. Apply OR rules
        if any_cfg:
            result_views = self._apply_any_rules(result_views, any_cfg)

        # 3. Apply exclude rules
        result_views = self._apply_exclude_rules(result_views, exclude_cfg)

        # --- map back to original objects ---
        return [view_to_original[id(v)] for v in result_views]

    def _to_selector_view(self, obj) -> DeviceSelectorView:
        """
        Adapt ORM objects or selector views to a selector-compatible view.

        This is the ONLY place where ORM-specific knowledge exists.
        """

        # Already selector-compatible
        if isinstance(obj, DeviceSelectorView):
            return obj

        # ORM Device
        try:
            return DeviceSelectorView(
                hostname=obj.hostname,
                labels=obj.labels or {},
                role=RoleView(obj.role.name),
            )
        except Exception as err:
            raise TypeError(f"Object of type {type(obj)} is not selector-compatible") from err

    # ------------------------------
    # Internal rule processors
    # ------------------------------

    def _apply_match_rules(self, devices: SelectorViews, cfg: dict) -> SelectorViews:
        """
        Apply AND logic: device must satisfy ALL match fields.
        """
        result: SelectorViews = devices
        for key, rule in cfg.items():
            handler = getattr(self, f"_match_{key}", None)
            if not handler:
                raise RuntimeError(f"Unknown selector rule: match.{key}")

            result = handler(result, rule)
        return result

    def _apply_any_rules(self, devices: SelectorViews, rules: list) -> SelectorViews:
        """
        Apply OR logic: device must satisfy AT LEAST ONE rule.

        Each rule is a dict with exactly one selector key,
        whose value is a selector rule dict, e.g.:
            {"role": {"include": ["PE"]}}
            {"hostname": {"include": ["CE*"]}}
            {"labels": {"fabric": "core"}}
        """
        matched_ids = set()
        matched = []

        for rule in rules:
            if not isinstance(rule, dict) or len(rule) != 1:
                raise RuntimeError(f"Invalid any-rule: expected single-key dict, got {rule!r}")

            dotted_key, value = next(iter(rule.items()))

            handler = self._resolve_handler("match", dotted_key)

            if not isinstance(value, dict):
                raise RuntimeError(
                    f"Invalid selector rule for '{dotted_key}': "
                    f"expected dict, got {type(value).__name__}"
                )

            for dev in handler(devices, value):
                dev_id = id(dev)
                if dev_id not in matched_ids:
                    matched_ids.add(dev_id)
                    matched.append(dev)

        return matched

    def _apply_exclude_rules(self, devices: SelectorViews, cfg: dict) -> SelectorViews:
        """
        Exclude devices matching any rule.
        """
        result = devices

        for key, rule in cfg.items():
            handler = getattr(self, f"_match_{key}", None)
            if not handler:
                raise RuntimeError(f"Unknown selector rule: exclude.{key}")

            excluded = handler(devices, rule)
            result = [d for d in result if d not in excluded]

        return result

    # ------------------------------
    # Rule handlers
    # ------------------------------
    @staticmethod
    def _match_role(devices: SelectorViews, rule: dict) -> SelectorViews:
        """
        Filter devices by role.

        Supported:
            role:
              include: ["PE", "RR"]
              exclude: ["CE"]
        """
        include = set(rule.get("include", []))
        exclude = set(rule.get("exclude", []))

        result = []

        for d in devices:
            role = d.role.name

            if include and role not in include:
                continue
            if role in exclude:
                continue

            result.append(d)

        return result

    @staticmethod
    def _match_labels(devices: SelectorViews, rule: dict) -> SelectorViews:
        """
        Filter devices by label key/value matches.

        Supported forms:

        Scalar equality:
            labels:
              fabric: fabric1

        Explicit include/exclude:
            labels:
              fabric:
                include: ["fabric1"]
                exclude: ["fabric2"]

        NOT supported:
            labels:
              fabric: ["fabric1"]
        """
        result = []

        for d in devices:
            dev_labels = d.labels or {}
            matched = True

            for label_key, selector in rule.items():
                if label_key not in dev_labels:
                    matched = False
                    break

                value = dev_labels[label_key]

                # Reject list shorthand explicitly
                if isinstance(selector, list):
                    raise RuntimeError(
                        f"List shorthand not supported for labels.{label_key}; "
                        "use scalar or include/exclude form"
                    )

                # Scalar shorthand
                if not isinstance(selector, dict):
                    if value != selector:
                        matched = False
                        break
                    continue

                include = set(selector.get("include", []))
                exclude = set(selector.get("exclude", []))

                if include and value not in include:
                    matched = False
                    break
                if value in exclude:
                    matched = False
                    break

            if matched:
                result.append(d)

        return result

    @staticmethod
    def _match_hostname(devices: SelectorViews, rule: dict) -> SelectorViews:
        """
        Filter devices by hostname using glob matching.

        Example:
            hostname:
              include: ["core-*"]
              exclude: ["core-test*"]
        """
        import fnmatch

        include = rule.get("include", [])
        exclude = rule.get("exclude", [])

        result = []

        for d in devices:
            hn = d.hostname

            if include and not any(fnmatch.fnmatch(hn, pat) for pat in include):
                continue
            if any(fnmatch.fnmatch(hn, pat) for pat in exclude):
                continue

            result.append(d)

        return result

    # ------------------------------
    # Helper for OR rules
    # ------------------------------
    def _resolve_handler(
        self,
        prefix: str,
        dotted_key: str,
    ) -> Callable[[SelectorViews, dict], SelectorViews]:
        """
        Resolve a handler method based on a dotted key like 'labels.fabric'
        or 'role.include' for use in OR rules.
        """
        key = dotted_key.split(".")[0]
        handler = getattr(self, f"_{prefix}_{key}", None)

        if not handler:
            raise RuntimeError(f"Unknown selector OR rule: {dotted_key}")

        return handler
