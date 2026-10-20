"""Platform-neutral identifiers and immutable result envelopes.

No Langfuse/LangSmith SDK types belong in this module. Runtime validation and
versioned wire DTOs will be introduced with the first business endpoints.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VersionRef:
    id: str
    version: int


@dataclass(frozen=True)
class ResultEnvelope:
    event_id: str
    project_id: str
    evaluation_id: str
    kind: str
    occurred_at: str
    payload: dict[str, Any]
    schema_version: int = 1
    sample_id: str | None = None
    attempt: int = 1
    sequence: int = 0
