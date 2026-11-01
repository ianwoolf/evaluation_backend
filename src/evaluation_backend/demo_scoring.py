"""Offline illustrative scoring. Never imports installed scoring plugins."""
import hashlib
import json
from evaluation_backend.experiment import Score, Scorer


class DemoScorerLibrary:
    def get_scorers(self, dataset, ids):
        def build(ident):
            def evaluate(**values):
                payload = json.dumps([dataset.id, ident, values], sort_keys=True, default=str)
                value = int(hashlib.sha256(payload.encode()).hexdigest()[:8], 16) / 0xffffffff
                return Score(round(value, 3), 'MOCK: illustrative demo score, not a quality assessment')
            return Scorer(ident, 'demo-v1', .8, evaluate)
        return [build(ident) for ident in ids]
