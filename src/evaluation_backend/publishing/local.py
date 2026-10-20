"""JSONL development sink. Not a production outbox or an idempotent transport."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from evaluation_backend.contracts import ResultEnvelope
from evaluation_backend.publishing.base import DeliveryReceipt


class LocalJsonlSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def publish(self, events: Sequence[ResultEnvelope]) -> list[DeliveryReceipt]:
        # Serialize first so an invalid payload does not write a partial batch.
        lines = [json.dumps(asdict(event), ensure_ascii=False, allow_nan=False) for event in events]
        if not lines:
            return []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        return [DeliveryReceipt(event_id=event.event_id) for event in events]
