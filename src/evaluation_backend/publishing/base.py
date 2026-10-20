"""Sink protocol. Durable outbox, retries and external ID mapping come later."""

from dataclasses import dataclass
from typing import Protocol, Sequence

from evaluation_backend.contracts import ResultEnvelope


@dataclass(frozen=True)
class DeliveryReceipt:
    event_id: str
    external_ref: str | None = None


class ResultSink(Protocol):
    def publish(self, events: Sequence[ResultEnvelope]) -> list[DeliveryReceipt]:
        """Return receipts for acknowledged events; raise on transport failure.

        Future remote adapters must preserve event identity across retries and
        document partial-success behavior. This API does not promise exactly-once.
        """
        ...
