"""Pydantic schemas for validating addressing policy YAML definitions."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class AddressingFeature(BaseModel):
    """IP addressing pools for one policy scope: loopback0, loopback1, and p2p pools by role."""

    model_config = ConfigDict(extra="forbid")

    loopback0: dict[str, Any]
    loopback1: dict[str, Any]
    p2p: dict[str, Any]


class AddressingPolicyDocument(BaseModel):
    """Top-level schema for an addressing policy YAML file. Extra keys are rejected."""

    model_config = ConfigDict(extra="forbid")

    name: str
    selectors: dict[str, Any]
    features: dict[str, AddressingFeature]
