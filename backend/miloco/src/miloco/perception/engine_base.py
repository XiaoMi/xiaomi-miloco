"""
BasePerceptionEngine — abstract interface for multimodal perception inference.
"""

from abc import ABC, abstractmethod

from miloco.perception.types import (
    BatchedSnapshot,
    OnDemandPerceptionResult,
    RealtimePerceptionResult,
)


class BasePerceptionEngine(ABC):
    """Abstract base class for perception engine."""

    @abstractmethod
    async def realtime_perceive(
        self,
        batch: BatchedSnapshot,
        rules: list[dict],
        dedupe_suggestions: bool = True,
    ) -> RealtimePerceptionResult | None:
        """Realtime perception — batch inference across all devices in one cycle.
        Receives a BatchedSnapshot containing multimodal data from all active
        devices collected within the same cycle window. The implementation can
        reason across devices simultaneously for cross-device scene understanding.
        Args:
            batch: All devices' data grouped by did for this perception cycle.
        Returns:
            RealtimePerceptionResult containing environment descriptions, matched rules, speeches, and suggestions.
        """

    @abstractmethod
    async def on_demand_perceive(self, batch: BatchedSnapshot, query: str) -> OnDemandPerceptionResult | None:
        """Active perception — answer a query using multi-device multimodal data.
        Receives a BatchedSnapshot (one or more devices) plus a natural language
        query. Supports multi-device fusion inference — the implementation can
        reason across all devices in the batch simultaneously.
        Args:
            batch: Devices' multimodal data grouped by did.
            query: Natural language question to answer.
        Returns:
            OnDemandPerceptionResult containing only the answer.
        """
