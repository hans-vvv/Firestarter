"""Abstract base for all feature handlers: provides session, service context, and logger."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging.logger import get_logger
from app.services.service_handling.service_builder import ServiceBuilder

logger = get_logger("app.feature_handlers")


class BaseFeatureHandler:
    """
    Base class for feature handlers responsible for computing feature-specific configuration.

    Attributes:
        session (Session): Database session for queries and persistence.

    """

    def __init__(
        self,
        *,
        session: Session,
        service_builder: ServiceBuilder,
    ):
        """Initialize with a DB session, feature config, and optional selectors."""
        self.session = session
        self.sb = service_builder

    def compute(self, svc_ctx: dict[str, Any]):
        """
        Abstract method to compute feature-specific context data.
        """
        raise NotImplementedError

    def log(self, msg: str) -> None:
        """Emit an INFO log line prefixed with the handler class name."""
        logger.info(f"[{type(self).__name__}] {msg}")
