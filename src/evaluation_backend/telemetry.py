"""Experiment identities and storage-backed LLM observation export."""
import os
import time
import uuid
from datetime import datetime, timezone


def prepare_experiment(env, fresh=False):
    ident = None if fresh else env.get('EXPERIMENT_ID')
    ident = ident or 'exp-' + uuid.uuid4().hex
    env['EXPERIMENT_ID'] = ident
    env['EVALUATE_ID'] = ident
    env['EVALUATION_ID'] = ident
    return ident


class StorageObservation:
    def __init__(self, client, evaluation_id, values):
        self.client, self.evaluation_id, self.values = client, evaluation_id, values
        self.id = 'llm-' + uuid.uuid4().hex
        self.started = time.perf_counter()
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.ended = False

    def update(self, **values):
        self.values.update({k: v.isoformat() if isinstance(v, datetime) else v for k, v in values.items()})

    def end(self):
        if self.ended:
            return
        from evaluation_backend.content_policy import ContentPolicy
        policy = ContentPolicy.from_env()
        self.client.trace({'id': self.id, 'evaluation_id': self.evaluation_id,
            'created_at': self.created_at, 'name': 'Execution trace' if policy.restricted else self.values.get('name', 'runner.llm'),
            'data': policy.observation({**self.values, 'duration_ms': (time.perf_counter() - self.started) * 1000})})
        self.ended = True


class StorageTelemetry:
    def __init__(self, env=None):
        from evaluation_backend.storage.client import StorageClient
        env = os.environ if env is None else env
        self.client = StorageClient(env)
        self.ident = env.get('EXPERIMENT_ID') or env.get('EVALUATE_ID')
        if not self.ident:
            raise ValueError('EXPERIMENT_ID is required for telemetry')

    def start_observation(self, **values):
        return StorageObservation(self.client, self.ident, values)

    def flush(self):
        pass  # Each finished observation is persisted synchronously.


def create_telemetry(env=None):
    return StorageTelemetry(env)
