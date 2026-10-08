"""Typed context containers passed between the orchestrator and feature handlers."""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel


class ServiceContext(TypedDict):
    """Bundles validated service instance data and service definition data for a feature handler."""

    svc_inst_data: BaseModel
    svc_def_data: BaseModel
