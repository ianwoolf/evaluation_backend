import json

import pytest

from evaluation_backend.contracts import ResultEnvelope
from evaluation_backend.publishing.local import LocalJsonlSink


def event(event_id, payload):
    return ResultEnvelope(
        event_id=event_id, project_id="project-1", evaluation_id="evaluation-1",
        kind="score.recorded", occurred_at="2026-10-19T00:00:00Z", payload=payload,
    )


def test_local_sink_preserves_internal_identity(tmp_path):
    path = tmp_path / "results.jsonl"
    receipts = LocalJsonlSink(path).publish([event("event-1", {"score": 1, "reason": "匹配"})])
    assert [receipt.event_id for receipt in receipts] == ["event-1"]
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["evaluation_id"] == "evaluation-1"
    assert record["payload"] == {"score": 1, "reason": "匹配"}


def test_invalid_batch_does_not_append_partial_results(tmp_path):
    path = tmp_path / "results.jsonl"
    with pytest.raises(ValueError):
        LocalJsonlSink(path).publish([event("ok", {"score": 1}), event("bad", {"score": float("nan")})])
    assert not path.exists()
