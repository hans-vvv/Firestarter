"""Abstract base class for all action-based job handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.job_handling.job_executor import JobExecutor


class BaseActionJobHandler:
    """
    Base class for action job handlers.

    Provides:
      - Access to the database session.
      - Helper method to update job steps safely.
    """

    def __init__(self, executor: JobExecutor):
        """Initialize with a reference to the JobExecutor."""
        self.executor = executor
        self.session = executor.session

    def identity(self, step: dict) -> str:
        """Abstract method to provide indentity for Job result"""
        raise NotImplementedError

    def handle(self, step: dict):
        """Abstract method to handle a job at a given step.
        Must be implemented by subclasses.
        """
        raise NotImplementedError
