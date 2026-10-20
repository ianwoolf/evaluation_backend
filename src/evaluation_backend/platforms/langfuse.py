"""Only this adapter imports Langfuse. SDK manages native traces/score feedback."""
import base64
import hashlib
import json
import os
from threading import Lock
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from evaluation_backend.experiment import Dataset, Settings, Score
from evaluation_backend.config import LangfuseConfig


class LangfusePlatform:
    def __init__(self, client=None, resolve_name=None, config=None):
        self.config = config
        if client is None:
            from langfuse import Langfuse
            self.config = config or LangfuseConfig.from_env()
            from evaluation_backend.telemetry import create_telemetry
            client = create_telemetry(self.config)
        self.client = client
        self.resolve_name = resolve_name or self._resolve_name
        self.datasets = {}

    def _resolve_name(self, dataset_id):
        config = self.config or LangfuseConfig.from_env()
        base = config.base_url
        auth = base64.b64encode((config.public_key + ':' + config.secret_key).encode()).decode()
        # SDK get_dataset takes a NAME, not an ID; resolve the ID explicitly.
        for page in range(1, 10001):
            request = Request(base + '/api/public/v2/datasets?' + urlencode({'page': page, 'limit': 100}), headers={'Authorization': 'Basic ' + auth})
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
            for row in payload['data']:
                if row['id'] == dataset_id:
                    return row['name']
            if page >= payload.get('meta', {}).get('totalPages', page if len(payload['data']) < 100 else page + 1):
                break
        raise ValueError('Langfuse dataset ID not found')

    def get_dataset(self, settings: Settings) -> Dataset:
        name = self.resolve_name(settings.dataset_id)
        raw = self.client.get_dataset(name, version=settings.dataset_version)
        if raw.id != settings.dataset_id:
            raise ValueError('dataset ID mismatch')
        metadata = raw.metadata or {}
        if not isinstance(metadata, dict):
            raise ValueError('dataset metadata must be an object')
        if not raw.items:
            raise ValueError('dataset has no active items')
        self.datasets[settings.dataset_id] = raw
        return Dataset(raw.id, raw.name, metadata)

    def execute(self, settings, dataset, target, scorers):
        lock = Lock()
        failure_count = 0
        def failed():
            nonlocal failure_count
            with lock:
                failure_count += 1
        def task(*, item, **kwargs):
            # Pass only input; never give target the full item/reference label.
            value = item['input'] if isinstance(item, dict) else item.input
            try:
                return target(value)
            except Exception:
                failed()
                raise ValueError('target execution failed') from None

        def evaluator(scorer):
            def evaluate(*, input, output, expected_output=None, metadata=None, **kwargs):
                reference = {'has_issue': expected_output} if type(expected_output) is bool else expected_output
                try:
                    score = scorer.evaluate(input=input, output=output, reference=reference, metadata=metadata)
                    if not isinstance(score, Score):
                        raise ValueError('scorer must return a Score')
                except Exception:
                    failed()
                    raise ValueError('scorer execution failed') from None
                return {'name': scorer.id, 'value': score.value,
                        'metadata': {'experiment_id': settings.evaluation_id, 'scorer_version': scorer.version, 'threshold': scorer.threshold, 'passed': score.value >= scorer.threshold},
                        'comment': f'{score.reason}; scorerVersion={scorer.version}; threshold={scorer.threshold}; passed={score.value >= scorer.threshold}'}
            evaluate.__name__ = scorer.id
            return evaluate

        raw = self.datasets[dataset.id]
        try:
            result = raw.run_experiment(
                name=settings.evaluation_id, run_name=settings.evaluation_id, task=task,
                evaluators=[evaluator(s) for s in scorers], max_concurrency=settings.concurrency,
                metadata={'experiment_id': settings.evaluation_id, 'evaluation_id': settings.evaluation_id, 'dataset_id': dataset.id,
                          'dataset_version': settings.dataset_version.isoformat(),
                          'scorer_versions': json.dumps({s.id: s.version for s in scorers}),
                          'scorer_config_hash': hashlib.sha256(json.dumps(dataset.metadata, sort_keys=True).encode()).hexdigest()})
            # SDK isolates item errors; do not mistake returned result for success.
            item_results = result.item_results
            errors = failure_count
            if len(item_results) != len(raw.items) or any(len(x.evaluations) != len(scorers) for x in item_results):
                errors = max(1, errors)
            return {'evaluation_id': settings.evaluation_id, 'dataset_id': dataset.id,
                    'status': 'FAILED' if errors else 'COMPLETED', 'items': len(item_results), 'errors': errors,
                    'url': result.dataset_run_url, 'langfuse_experiment_id': result.experiment_id}
        finally:
            self.client.flush()
