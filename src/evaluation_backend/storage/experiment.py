"""Platform-independent target/scorer execution with per-sample durable feedback."""
from evaluation_backend.observability import observed
from copy import deepcopy
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from evaluation_backend.experiment import Dataset, Score
from .client import StorageClient
from evaluation_backend.observability import event, operation, log_context


class StoredExperiment:
    def __init__(self, client=None, version=None):
        self.client = client or StorageClient()
        self.version = int(version or os.environ.get('EVAL_DATASET_VERSION', '1'))
        self.content = None

    @observed('dataset.load')
    def get_dataset(self, settings):
        run_input = os.getenv('EVAL_RUN_INPUT_ID')
        self.content = self.client.run_input(run_input) if run_input else self.client.version(settings.dataset_id, self.version)
        if not self.content['rows']:
            raise ValueError('Dataset has no published records')
        event('dataset.loaded', dataset_id=settings.dataset_id, run_id=settings.evaluation_id, count=len(self.content['rows']))
        return Dataset(settings.dataset_id, self.content['name'], self.content['metadata'])

    @observed('evaluation.execute')
    def execute(self, settings, dataset, target, scorers):
        content = self.content
        summary = {'dataset_id': dataset.id, 'dataset_version': self.version, 'items': 0, 'errors': 0, 'status': 'RUNNING'}
        self.client.experiment(settings.evaluation_id, summary)

        def sample(entry):
            index, row = entry
            fields = content['fields']
            def select(role):
                return {f['name']: row[f['name']] for f in fields if f['role'] == role and f['name'] in row}
            value, reference, metadata = select('input'), select('reference'), select('metadata')
            result = {'id': f'sample-{index:08d}', 'input': value, 'reference': reference, 'output': {},
                      'status': 'ERROR', 'error': None, 'scores': [], 'trace': [], 'costUsd': 0, 'modelLatencyMs': 0}
            started = time.perf_counter()
            stage, scorer_id = 'target', None
            event('sample.started', run_id=settings.evaluation_id, sample_id=result['id'])
            try:
                if os.getenv('EVAL_MODEL_ID') == 'none':
                    result['output'] = deepcopy(value)
                else:
                    before = time.perf_counter()
                    try:
                        with operation('target.execute', run_id=settings.evaluation_id, sample_id=result['id']):
                            result['output'] = target(value)
                    finally:
                        result['modelLatencyMs'] = (time.perf_counter() - before) * 1000
                    if not isinstance(result['output'], dict):
                        raise ValueError('Target must return an object')
                    result['trace'].append({'name': 'Target', 'kind': 'GUARDRAIL',
                        'durationMs': result['modelLatencyMs'], 'detail': 'Target completed'})
                for scorer in scorers:
                    stage, scorer_id = 'scorer', scorer.id
                    before = time.perf_counter()
                    with operation('scorer.execute', run_id=settings.evaluation_id, sample_id=result['id'], scorer_id=scorer.id):
                        score = scorer.evaluate(input=value, output=result['output'], reference=reference, metadata=metadata)
                    if not isinstance(score, Score):
                        raise ValueError('Scorer must return Score')
                    result['scores'].append({'scorerId': scorer.id, 'scorerVersion': scorer.version,
                        'value': score.value, 'passed': score.value >= scorer.threshold, 'reason': score.reason})
                    result['trace'].append({'name': scorer.id, 'kind': 'SCORER',
                        'durationMs': (time.perf_counter() - before) * 1000, 'detail': score.reason})
                result['status'] = 'PASS' if all(s['passed'] for s in result['scores']) else 'FAIL'
            except Exception as error:
                event('sample.failed', level='ERROR', error=error, run_id=settings.evaluation_id,
                      sample_id=result['id'], phase=stage, scorer_id=scorer_id)
                # Do not persist exception strings which may contain auth headers.
                result['error'] = type(error).__name__
                if not isinstance(result['output'], dict):
                    result['output'] = {}
            result['latencyMs'] = (time.perf_counter() - started) * 1000
            # Persistence errors propagate and fail the Job instead of losing results silently.
            with log_context(run_id=settings.evaluation_id, sample_id=result['id']):
                self.client.sample(settings.evaluation_id, result)
            event('sample.completed', run_id=settings.evaluation_id, sample_id=result['id'],
                  status=result['status'], duration_ms=round(result['latencyMs'], 2))
            return result['status']

        try:
            with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
                for status in pool.map(sample, enumerate(content['rows'])):
                    summary['items'] += 1
                    summary['errors'] += status == 'ERROR'
            summary['status'] = 'FAILED' if summary['errors'] else 'COMPLETED'
        except Exception:
            summary['status'] = 'FAILED'
            summary['errors'] += 1
            self.client.experiment(settings.evaluation_id, summary)
            raise
        self.client.experiment(settings.evaluation_id, summary)
        return {**summary, 'evaluation_id': settings.evaluation_id}
