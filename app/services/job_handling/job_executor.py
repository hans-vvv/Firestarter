"""Executes a Job by dispatching each action to the appropriate job handler."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.logging.logger import get_logger
from app.models import Job
from app.services.job_handling import (
    AddCeJobHandler,
    AddDeviceJobHandler,
    AddP2PConnectionJobHandler,
    AddPEPairJobHandler,
)
from app.services.selectors.selector_engine import SelectorEngine
from app.services.service_handling.addressing_policy_resolver import (
    AddressingPolicyResolver,
)
from app.services.service_handling.resource_pool_allocator import ResourcePoolAllocator
from app.services.service_handling.service_builder import ServiceBuilder
from app.services.service_handling.service_orchestrator import ServiceOrchestrator
from app.services.topology_building.cable_builder import CableBuilder
from app.services.topology_building.ce_attachment_builder import CEAttachmentBuilder
from app.services.topology_building.device_builder import DeviceBuilder
from app.services.topology_building.device_factory import DeviceFactory
from app.services.topology_building.pe_pair_builder import PEPairBuilder
from app.services.topology_building.topology_builder import TopologyBuilder
from app.utils import require

logger = get_logger("app.job_executor")


class JobExecutor:
    """
    Executes job steps by dispatching them to appropriate handlers and managing service recomputations.

    Responsibilities:
    - Executing jobs ,resulting in topology changes.
    - Initialize all required handlers and builders.
    - Execute job steps sequentially.
    """

    def __init__(self, session: Session):
        """Initialize with a database session and prepare handlers/builders."""
        self.session = session

        self.selector_engine = SelectorEngine(session=session)
        self.resource_pool_allocator = ResourcePoolAllocator(session=session)
        self.device_factory = DeviceFactory(session=session)
        self.cable_builder = CableBuilder(session=session)

        self.service_builder = ServiceBuilder(
            session=session,
            selector_engine=self.selector_engine,
            resource_pool_allocator=self.resource_pool_allocator,
        )
        self.device_builder = DeviceBuilder(
            session=session,
            device_factory=self.device_factory,
            prefix_allocator=self.resource_pool_allocator,
        )
        self.topology_builder = TopologyBuilder(
            session=session,
            prefix_allocator=self.resource_pool_allocator,
            cable_builder=self.cable_builder,
            device_builder=self.device_builder,
        )
        self.pe_pair_builder = PEPairBuilder(
            session=session,
            topology_builder=self.topology_builder,
            device_builder=self.device_builder,
        )
        self.ce_attachment_builder = CEAttachmentBuilder(
            session=session,
            topology_builder=self.topology_builder,
        )
        self.addressing_policy_resolver = AddressingPolicyResolver(
            selector_engine=self.selector_engine
        )
        self.service_orchestrator = ServiceOrchestrator(service_builder=self.service_builder)

        self.handlers = {
            "add_device": AddDeviceJobHandler(self),
            "add_cable": AddP2PConnectionJobHandler(self),
            "add_pe_pair": AddPEPairJobHandler(self),
            "attach_ce": AddCeJobHandler(self),
        }

    def execute(self, job: Job) -> bool:
        """
        Run all actions in a job
        Dispatch each step to its handler and collect results.

        Raises:
            RuntimeError: On unknown action or failure during step handling.
        """

        # Install IP addressing policies based on YAML intent
        self.addressing_policy_resolver.install()

        actions = require(job.actions_blob, "actions_blob missing")
        logger.info(f"Job '{job.name}' started — {len(actions)} step(s)")

        job_results = {}
        topology_changed = False

        for step in actions:
            action = step["action"]
            handler = self.handlers.get(step["action"])
            if not handler:
                raise RuntimeError(f"Unknown action: {step['action']}")

            result = handler.handle(step)
            status = result.get("status", "done")
            if status == "exists":
                logger.info(
                    f"  Step [{action}] — {handler.identity(step)} — skipped (already exists)"
                )
            else:
                logger.info(f"  Step [{action}] — {handler.identity(step)} — {status}")
            topology_changed = topology_changed or result.get("topology_changed", False)
            job_results[f"{action}:{handler.identity(step)}"] = result

        job.status = "completed"
        job.executed_at = datetime.now(UTC)
        logger.info(f"Job '{job.name}' completed")

        return topology_changed
