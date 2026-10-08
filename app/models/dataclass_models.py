"""Lightweight read-only dataclasses used as view projections across the service layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class RoleView:
    """Immutable projection of a Role used inside selector logic."""

    name: str


@dataclass(frozen=True)
class DeviceSelectorView:
    """Immutable device snapshot passed to SelectorEngine for rule evaluation."""

    hostname: str
    labels: Mapping[str, str]
    role: RoleView


@dataclass(frozen=True, slots=True)
class ServiceDescriptor:
    """
    Describes a service type known to the Service Orchestrator.

    - name: Service name (i.e.: isis, bgp, etc)
    - definition_schema: Pydantic model for service definition validation
    - feature_handlers: mapping of feature-name -> handler import path
    """

    name: str
    definition_schema: type[BaseModel]
    feature_handlers: dict[str, str]
